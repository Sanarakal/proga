import asyncio
import concurrent.futures
import json
import os
import re
import sys
import threading
import time

from PySide6.QtCore import QObject, Signal
from pymax import WebClient
from pymax.exceptions import ApiError
from pymax.protocol.enums import Opcode, Command

from workspace_security import Vault
from workspace_auth import PhoneAuthFlow, SavedSessionFlow, WorkspaceQrAuthFlow, login_error, normalize_phone
from workspace_status import classify_account_error


def safe_error(error):
    if isinstance(error, ApiError):
        code = error.error or 'unknown'
        detail = error.localized_message or error.message or 'MAX отклонил запрос'
        if 'too-many' in code.lower() or 'flood' in code.lower():
            detail = 'MAX ограничил запросы. Задание приостановлено; срок ограничения сервер не сообщил.'
        text = f'{detail} (код: {code}, операция: {error.opcode})'
    elif isinstance(error, (ValueError, JobPaused, JobStopped)):
        text = str(error)
    elif isinstance(error, TimeoutError):
        text = 'Ответ MAX не получен вовремя. Проверьте результат перед повтором.'
    else:
        text = f'Операция не завершена: {type(error).__name__}. Проверьте подключение и права MAX.'
    text = re.sub(r'https?://\S+', '[ссылка скрыта]', text)
    text = re.sub(r'\+?\d[\d ()-]{7,}\d', '[номер скрыт]', text)
    text = re.sub(r'(?i)(token|password|authorization)\s*[:=]\s*\S+', r'\1=[скрыто]', text)
    return text[:700]


def user_name(user):
    for name in getattr(user, 'names', []) or []:
        if getattr(name, 'name', None):
            return name.name
        parts = [getattr(name, key, None) for key in ('first_name', 'last_name')]
        if any(parts):
            return ' '.join(part for part in parts if part)
    return f'Участник {user.id}'


def unavailable_contact(error):
    # Only explicit participant errors; generic forbidden/blocked may concern our account.
    code = (error.error or '').lower()
    return code in {'not.found', 'errors.not.found', 'user.not.found',
                    'contact.not.found', 'user.blocked', 'contact.blocked',
                    'errors.user.blocked', 'errors.contact.blocked',
                    'user.deleted', 'contact.deleted', 'participants.filter.out'}


def blocked_user(user):
    status = str(getattr(user, 'status', '') or '').lower()
    options = ' '.join(str(value).lower() for value in (getattr(user, 'options', None) or []))
    return any(marker in f'{status} {options}' for marker in ('blocked', 'deleted', 'заблокирован'))


def chat_participant_ids(chat):
    if not chat:
        return set()
    values = set()
    for collection in (getattr(chat, 'participants', None),
                       getattr(chat, 'admin_participants', None)):
        if isinstance(collection, dict):
            values.update(collection.keys())
    values.update(getattr(chat, 'admins', None) or [])
    owner = getattr(chat, 'owner', None)
    if owner:
        values.add(owner)
    result = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


class JobPaused(Exception):
    pass


class JobStopped(Exception):
    pass


class LoginProvider:
    def __init__(self, engine, account):
        self.engine, self.account = engine, account
        self.password_attempts = 0
        self.interactive_started = False

    async def show_qr(self, url):
        self.interactive_started = True
        self.engine.event.emit('qr', (self.account, url))

    async def answer(self, kind, detail=''):
        self.interactive_started = True
        answer = concurrent.futures.Future()
        self.engine.event.emit(kind, (self.account, answer, detail))
        result = await asyncio.wrap_future(answer)
        if not result:
            raise ValueError('Вход отменён')
        return result

    async def get_code(self, phone):
        code = await self.answer('code', phone[:2] + ' *** *** ' + phone[-4:])
        if not re.fullmatch(r'[0-9]{4,10}', code):
            raise ValueError('Код должен содержать от 4 до 10 цифр')
        return code

    async def get_password(self, hint=None):
        self.password_attempts += 1
        if self.password_attempts > 1:
            raise ValueError('MAX не подтвердил пароль. Повторите вход вручную.')
        return await self.answer('password')


class Engine(QObject):
    event = Signal(str, object)

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.vault = Vault(store.directory / 'sessions')
        self.clients = {}
        self.futures = {}
        self.flags = {}
        self.cache = {}
        self.last_request = {}
        self.metrics = {}
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.worker, daemon=True)
        self.thread.start()

    def worker(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self.watch_connections())
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def connected(self, account):
        client = self.clients.get(account)
        return client is not None and getattr(client, 'is_connected', True) is not False

    def available(self, account):
        if not self.connected(account):
            return False
        rows = self.store.rows('SELECT connection_state,blocked_until FROM accounts WHERE id=?', (account,))
        return bool(rows and rows[0]['connection_state'] not in ('needs_login', 'blocked', 'connecting')
                    and rows[0]['blocked_until'] <= time.time())

    def set_account_status(self, account, state, detail='', checked=False):
        if self.store.account_status(account, state, detail, checked):
            self.event.emit('changed', account)

    def record_account_error(self, account, error):
        result = classify_account_error(error)
        if result:
            state, detail = result
            current = self.store.rows('SELECT connection_state FROM accounts WHERE id=?', (account,))
            if not current:
                return result
            if current[0]['connection_state'] == 'blocked' and state != 'blocked':
                return result
            # A later transport failure must not hide an explicit logout or restriction.
            if state == 'network_error' and current[0]['connection_state'] in ('needs_login', 'blocked', 'limited'):
                return result
            if state == 'limited':
                self.store.execute('UPDATE accounts SET blocked_until=MAX(blocked_until,?) WHERE id=?',
                                   (time.time() + 300, account))
            self.set_account_status(account, state, detail, checked=isinstance(error, ApiError))
        return result

    async def watch_connections(self):
        while True:
            await asyncio.sleep(2)
            await self.inspect_connections()

    async def inspect_connections(self):
        for account, client in list(self.clients.items()):
            try:
                rows = self.store.rows('SELECT connection_state FROM accounts WHERE id=?', (account,))
                unavailable = rows and rows[0]['connection_state'] in ('needs_login', 'blocked')
                if not self.connected(account):
                    self.record_account_error(account, ConnectionError())
                    unavailable = True
                if unavailable and not self.busy(account) and self.clients.get(account) is client:
                    await self.disconnect(account, preserve_status=True)
            except Exception as error:
                self.store.log(account, safe_error(error))

    async def account_event(self, account, client, frame):
        if self.clients.get(account) is not client:
            return
        if frame.cmd == Command.EVENT and frame.opcode == Opcode.LOGOUT:
            self.set_account_status(account, 'needs_login', 'MAX завершил сессию. Подключите аккаунт заново.', checked=True)
        elif frame.cmd == Command.ERROR:
            payload = frame.payload if isinstance(frame.payload, dict) else {}
            code = payload.get('error')
            if isinstance(code, str):
                self.record_account_error(account, ApiError(opcode=frame.opcode, error=code))
        elif frame.cmd == Command.RESPONSE and frame.opcode == Opcode.PING:
            self.store.execute('UPDATE accounts SET status_checked=? WHERE id=?', (time.time(), account))
            rows = self.store.rows('SELECT connection_state FROM accounts WHERE id=?', (account,))
            if rows and rows[0]['connection_state'] == 'network_error':
                self.set_account_status(account, 'online', 'Соединение с MAX восстановлено.', checked=True)

    async def check_account(self, account):
        if not self.connected(account):
            if account in self.clients:
                self.record_account_error(account, ConnectionError())
            return 'Нет соединения. Подключите аккаунт для проверки в MAX.'
        try:
            await asyncio.wait_for(self.clients[account]._app.invoke(Opcode.PING, {'interactive': True}), 15)
        except Exception as error:
            if not self.record_account_error(account, error):
                row = self.store.rows('SELECT connection_state FROM accounts WHERE id=?', (account,))[0]
                if row['connection_state'] not in ('needs_login', 'blocked', 'limited'):
                    self.set_account_status(account, 'error', 'Не удалось проверить аккаунт. Повторите проверку позже.')
            raise
        row = self.store.rows('SELECT connection_state FROM accounts WHERE id=?', (account,))[0]
        if row['connection_state'] not in ('needs_login', 'blocked', 'limited'):
            self.set_account_status(account, 'online', 'Соединение с MAX подтверждено.', checked=True)
        else:
            self.store.execute('UPDATE accounts SET status_checked=? WHERE id=?', (time.time(), account))
        return 'MAX ответил. Статус аккаунта обновлён.'

    def busy(self, account):
        return account in self.futures and not self.futures[account].done()

    def submit(self, account, tag, operation):
        if self.busy(account):
            operation.close()
            raise ValueError('Дождитесь завершения текущей операции этого аккаунта')
        future = asyncio.run_coroutine_threadsafe(operation, self.loop)
        self.futures[account] = future
        self.event.emit('busy', account)
        def done(result):
            try:
                value = result.result()
                self.event.emit('result', (account, tag, value))
            except concurrent.futures.CancelledError:
                self.event.emit('error', (account, 'Операция отменена'))
            except Exception as error:
                text = safe_error(error)
                self.store.log(account, text)
                self.event.emit('error', (account, text))
            self.event.emit('changed', account)
        future.add_done_callback(done)
        return future

    def checkpoint(self, job):
        action = self.flags.get(job)
        if action == 'pause':
            raise JobPaused('Задание приостановлено пользователем')
        if action == 'stop':
            raise JobStopped('Задание остановлено пользователем')

    def job_report(self, job_or_id):
        job = self.store.job(job_or_id) if isinstance(job_or_id, str) else job_or_id
        if job['kind'] == 'broadcast':
            from workspace_broadcast import broadcast_report
            return broadcast_report(self.store, job['id'])
        if job['kind'] == 'join':
            rows = self.store.join_rows(job['id'])
            counts = {state: sum(r['state'] == state for r in rows) for state in
                      ('confirmed', 'skipped', 'unavailable', 'pending', 'retryable', 'queued', 'global_skip', 'reserved_skip')}
            options = json.loads(job.get('options') or '{}')
            return '\n'.join([
                f'Вступил: {counts["confirmed"]} из {job["amount"]}',
                f'Уже состоит / в истории: {counts["skipped"]}',
                f'В общем реестре: {counts["global_skip"]}; закреплено другим заданием: {counts["reserved_skip"]}',
                f'Недоступные группы: {counts["unavailable"]}',
                f'Не подтверждено: {counts["pending"]}',
                f'Ошибки, повтор вручную: {counts["retryable"]}',
                f'Не обработано ссылок: {counts["queued"]}',
                f'Исключено при импорте: {options.get("invalid", 0)}; повторов: {options.get("duplicates", 0)}',
            ])
        states = self.store.items(job['id'])
        counts = {state: sum(value == state for value in states.values()) for state in
                  ('confirmed', 'unavailable', 'skipped', 'retryable', 'pending')}
        done = counts['confirmed']
        missed = max(0, job['amount'] - done)
        if job['kind'] == 'invite':
            action = 'Добавлено в группу'
        elif job['kind'] == 'collect':
            action = 'Добавлено в контакты'
        else:
            action = 'Переслано в чаты'
        lines = [f'{action}: {done} из {job["amount"]}', f'Не выполнено: {missed}']
        if counts['unavailable']:
            lines.append(f'Недоступны или запрещено приватностью: {counts["unavailable"]}')
        ignored = self.store.ignored_ids()
        blocked_count = sum(uid in ignored and state == 'unavailable' for uid, state in states.items())
        if blocked_count:
            lines.append(f'Заблокированы / удалены, в игноре: {blocked_count}')
        if counts['skipped']:
            reason = 'Уже были в группе / пропущены' if job['kind'] == 'invite' else 'Уже были добавлены / пропущены'
            lines.append(f'{reason}: {counts["skipped"]}')
        if counts['retryable']:
            lines.append(f'Ошибки, для которых разрешён повтор: {counts["retryable"]}')
        if counts['pending']:
            lines.append(f'Требуется ручная проверка: {counts["pending"]}')
        attempts = sum(row['attempts'] for row in self.store.item_rows(job['id']))
        if attempts:
            lines.append(f'Попыток отправки: {attempts}')
        if job['kind'] == 'collect' and job['chat']:
            sources = [row for row in self.store.source_rows() if row['chat'] == job['chat']]
            if sources:
                source = sources[0]
                total = source['total'] if source['total'] > 0 else '?'
                lines.append(f'Источник: {source["title"]}')
                lines.append(f'Всего взято из источника: {source["taken"]} из {total}')
        accounted = sum(counts.values())
        unprocessed = max(0, min(job['amount'], len(json.loads(job['candidates']))) - accounted)
        if unprocessed:
            lines.append(f'Не обработано: {unprocessed}')
        if done == 0 and job['kind'] == 'invite':
            lines.append('Итог: ни один участник не добавлен.')
        recorded = self.store.rows('SELECT * FROM job_metrics WHERE job=?', (job['id'],))
        totals = dict(recorded[0]) if recorded else dict(elapsed=0, waiting=0, network=0, requests=0, cached_pages=0)
        live = self.metrics.get(job['account'])
        if live and live['job'] == job['id']:
            for key in ('waiting', 'network', 'requests', 'cached_pages'):
                totals[key] += live[key]
            totals['elapsed'] += time.monotonic() - live['started']
        if totals['elapsed']:
            elapsed = totals['elapsed']
            local = max(0, elapsed - totals['waiting'] - totals['network'])
            lines.append(f'Время работы: {elapsed:.1f} с; скорость: {done * 60 / elapsed:.1f} контактов/мин')
            lines.append(f'Интервалы: {totals["waiting"]:.1f} с; ответы MAX: {totals["network"]:.1f} с; обработка: {local:.1f} с')
            lines.append(f'Вызовов API: {totals["requests"]}; страниц из кэша: {totals["cached_pages"]}')
        return '\n'.join(lines)

    async def request(self, account, operation, *args, _timeout=45, **kwargs):
        row = self.store.rows('SELECT blocked_until,connection_state FROM accounts WHERE id=?', (account,))[0]
        if row['connection_state'] in ('needs_login', 'blocked'):
            raise JobPaused('Аккаунт недоступен: ' + ('требуется вход' if row['connection_state'] == 'needs_login' else 'заблокирован'))
        if row['blocked_until'] > time.time():
            raise JobPaused('Локальная пауза после ограничения MAX ещё не закончилась. Автоматических повторов нет.')
        reading = getattr(operation, '__name__', '').startswith(('get_', 'fetch_', 'search_', 'resolve_'))
        setting = 'read_delay' if reading else 'request_delay'
        delay = max(1, min(30, float(self.store.setting(setting, '2'))))
        key = (account, 'read' if reading else 'write')
        remaining = delay - (time.monotonic() - self.last_request.get(key, 0))
        metrics = self.metrics.get(account)
        if remaining > 0:
            start = time.monotonic()
            try:
                await asyncio.sleep(remaining)
            finally:
                if metrics:
                    metrics['waiting'] += time.monotonic() - start
        latest = self.store.rows('SELECT connection_state,blocked_until FROM accounts WHERE id=?', (account,))[0]
        if latest['connection_state'] in ('needs_login', 'blocked') or latest['blocked_until'] > time.time():
            raise JobPaused('Статус аккаунта изменился. Проверьте аккаунт перед продолжением задания.')
        self.last_request[key] = time.monotonic()
        start = time.monotonic()
        if metrics:
            metrics['requests'] += 1
        try:
            result = await asyncio.wait_for(operation(*args, **kwargs), _timeout)
            if row['connection_state'] in ('limited', 'network_error', 'error'):
                current = self.store.rows('SELECT connection_state,blocked_until FROM accounts WHERE id=?', (account,))[0]
                if current['connection_state'] not in ('blocked', 'needs_login') and current['blocked_until'] <= time.time():
                    self.set_account_status(account, 'online', 'Запрос к MAX выполнен.', checked=True)
            return result
        except Exception as error:
            self.record_account_error(account, error)
            raise
        finally:
            if metrics:
                metrics['network'] += time.monotonic() - start

    def persist_chats(self, account, chats):
        for chat in chats or []:
            kind = str(getattr(chat.type, 'value', chat.type))
            if kind in ('CHAT', 'CHANNEL', 'DIALOG'):
                self.store.execute('INSERT INTO chats VALUES(?,?,?,?) ON CONFLICT(account,uid) DO UPDATE SET name=excluded.name,kind=excluded.kind', (account, chat.id, chat.title or 'Без названия', kind))
                own = self.store.rows('SELECT max_id FROM accounts WHERE id=?', (account,))[0]['max_id']
                admins = set(getattr(chat, 'admins', None) or []) | set((getattr(chat, 'admin_participants', None) or {}).keys())
                allowed = own is not None and (own == getattr(chat, 'owner', None) or own in admins)
                self.store.execute('INSERT OR REPLACE INTO chat_roles VALUES(?,?,?)', (account, chat.id, int(allowed)))

    async def connect(self, account, method='qr', phone=''):
        previous_status = self.store.rows('SELECT connection_state,status_detail FROM accounts WHERE id=?', (account,))[0]
        if self.connected(account) and previous_status['connection_state'] not in ('needs_login', 'blocked'):
            return 'Аккаунт уже подключён'
        if account in self.clients:
            await self.disconnect(account, preserve_status=True)
        if method not in ('qr', 'phone', 'saved'):
            raise ValueError('Неизвестный способ входа')
        if method == 'phone':
            phone = normalize_phone(phone)
        self.set_account_status(account, 'connecting', 'Ожидание входа в MAX.')
        if sys.stderr is None:
            sys.stderr = open(os.devnull, 'w', encoding='utf-8')
        provider = LoginProvider(self, account)
        client = None
        try:
            folder = self.vault.restore(account)
            flow = (PhoneAuthFlow(phone, provider) if method == 'phone' else
                    SavedSessionFlow() if method == 'saved' else WorkspaceQrAuthFlow(provider, provider))
            client = WebClient(work_dir=str(folder), session_name='session.db', auth_flow=flow)
            async with asyncio.timeout(240):
                try:
                    await client.connect()
                except ApiError as error:
                    revoked = error.opcode == Opcode.LOGIN and any(
                        marker in (error.error, error.message)
                        for marker in ('FAIL_LOGIN_TOKEN', 'FAIL_LOGOUT_ALL'))
                    if not revoked or provider.interactive_started:
                        raise
                    # Reset only a server-revoked session, never on transport or code errors.
                    await client.relogin(start=False)
                    if method == 'saved':
                        raise ValueError('Сессия отозвана в MAX. Выберите вход по QR-коду или номеру телефона.') from None
                    await client.connect()
            if not client.me:
                raise ValueError('MAX не подтвердил вход')
            if getattr(client, 'is_connected', True) is False:
                raise ConnectionError('MAX disconnected during login')
            own_id = client.me.contact.id
            previous = self.store.rows('SELECT max_id FROM accounts WHERE id=?', (account,))[0]['max_id']
            if previous is not None and previous != own_id:
                raise ValueError('Выполнен вход в другой аккаунт MAX. Создайте для него отдельную запись.')
            other = self.store.rows('SELECT id FROM accounts WHERE max_id=? AND id<>?', (own_id, account))
            if other:
                raise ValueError('Этот аккаунт MAX уже есть в списке. Подключите существующую запись.')
            self.store.execute('''UPDATE accounts SET max_id=?,last_connected=?,last_error=''
                WHERE id=?''', (own_id, time.time(), account))
            for contact in client.contacts or []:
                if contact:
                    self.store.contact(account, contact.id, user_name(contact))
            self.persist_chats(account, client.chats)
            self.clients[account] = client
            if hasattr(client, 'on_raw'):
                async def on_frame(frame, source):
                    await self.account_event(account, client, frame)
                client.on_raw()(on_frame)
            self.set_account_status(account, 'online', 'Вход в MAX подтверждён.', checked=True)
            self.store.log(account, 'Аккаунт подключён')
            return 'Аккаунт подключён'
        except BaseException as error:
            if self.clients.get(account) is client:
                self.clients.pop(account, None)
            clean = login_error(error)
            classified = self.record_account_error(account, error)
            if (not classified or classified[0] == 'network_error') and previous_status['connection_state'] in ('blocked', 'limited'):
                self.set_account_status(account, previous_status['connection_state'], previous_status['status_detail'])
            elif not classified:
                state = 'offline' if isinstance(error, asyncio.CancelledError) else 'needs_login'
                self.set_account_status(account, state, 'Вход отменён.' if state == 'offline' else safe_error(clean))
            self.store.execute('UPDATE accounts SET last_error=? WHERE id=?', (safe_error(clean), account))
            try:
                if client:
                    await client.close()
            finally:
                self.vault.seal(account)
            if clean is error:
                raise
            raise clean from None

    async def disconnect(self, account, preserve_status=False):
        client = self.clients.pop(account, None)
        try:
            if client:
                await client.close()
        finally:
            self.vault.seal(account)
        self.cache = {key: value for key, value in self.cache.items() if key[0] != account}
        rows = self.store.rows('SELECT connection_state FROM accounts WHERE id=?', (account,))
        if not preserve_status and rows and rows[0]['connection_state'] not in ('needs_login', 'blocked', 'limited'):
            self.set_account_status(account, 'offline', 'Отключён в приложении. Сессия сохранена.')
        return 'Аккаунт отключён; сессия защищена Windows'

    async def refresh(self, account):
        client = self.clients[account]
        seen = set()
        marker = None
        for _ in range(2000):
            chats = await self.request(account, client.fetch_chats, marker=marker)
            if not chats:
                break
            self.persist_chats(account, chats)
            times = [chat.last_event_time for chat in chats if chat.last_event_time]
            if not times:
                break
            next_marker = min(times) - 1
            if next_marker in seen or (marker is not None and next_marker >= marker):
                break
            seen.add(next_marker)
            marker = next_marker
        else:
            raise ValueError('Список чатов слишком большой; загрузка остановлена')
        self.store.execute('UPDATE accounts SET chats_synced=? WHERE id=?', (time.time(), account))
        return 'Список чатов обновлён'

    async def members(self, account, chat, fresh=False, job=None):
        key = (account, chat)
        cached = self.cache.get(key)
        if not fresh and cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        client = self.clients[account]
        info = await self.request(account, client.get_chat, chat)
        expected = getattr(info, 'participants_count', 0) or 0
        result = {uid: '' for uid in chat_participant_ids(info)}
        markers = set()
        marker = None
        for _ in range(2000):
            if job:
                self.checkpoint(job)
            members, marker_next = await self.request(account, client.get_chat_members, chat, marker=marker, count=50)
            for member in members:
                result[member.contact.id] = user_name(member.contact)
            if not marker_next:
                if expected > len(result):
                    # MAX may include hidden, deleted and service profiles in participants_count
                    # while omitting them from the completed member pagination.
                    self.store.log(
                        account,
                        f'Чат ID {chat}: MAX указал {expected} участников, API вернул '
                        f'{len(result)} видимых; работа продолжается по видимому списку')
                self.cache[key] = (time.monotonic(), result)
                return result
            if marker_next in markers:
                raise ValueError('MAX повторяет страницу участников. Проверка не завершена; приглашения не начаты.')
            markers.add(marker_next)
            marker = marker_next
        raise ValueError('Полный список участников не прочитан')

    async def collection_candidates(self, account, chat, amount, excluded):
        result = {}
        async for batch in self.collection_pages(account, chat):
            for uid, name, blocked in batch:
                if uid not in excluded:
                    result[uid] = name
            if len(result) >= amount:
                return result
        return result

    async def collection_pages(self, account, chat, job=None):
        client = self.clients[account]
        info = next((entry for entry in (getattr(client, 'chats', None) or []) if entry.id == chat), None)
        if info is None:
            info = await self.request(account, client.get_chat, chat)
        self.store.update_source(chat, getattr(info, 'title', '') or '', getattr(info, 'participants_count', 0) or 0)
        if not self.store.source_page(account, chat, None):
            self.store.clear_source_pages(account, chat)
        marker, markers, restarted = None, set(), False
        for _ in range(2000):
            if job:
                self.checkpoint(job)
            cached = self.store.source_page(account, chat, marker)
            if cached:
                batch, next_marker = json.loads(cached['members']), cached['next_marker']
                if account in self.metrics:
                    self.metrics[account]['cached_pages'] += 1
            else:
                try:
                    members, next_marker = await self.request(account, client.get_chat_members, chat, marker=marker, count=50)
                except ApiError as error:
                    code = (error.error or '').lower()
                    if marker and not restarted and code in ('invalid.marker', 'marker.invalid', 'marker.expired'):
                        self.store.clear_source_pages(account, chat)
                        marker, markers, restarted = None, set(), True
                        continue
                    raise
                batch = [(m.contact.id, user_name(m.contact), blocked_user(m.contact)) for m in members]
                self.store.save_source_page(account, chat, marker, batch, next_marker)
            yield batch
            if not next_marker:
                return
            if next_marker in markers:
                raise ValueError('MAX повторяет страницу участников')
            markers.add(next_marker)
            marker = next_marker
        raise ValueError('Список участников слишком большой')

    async def preview(self, account, kind, chat, amount=None, label=''):
        own = self.clients[account].me.contact.id
        rows = self.store.contacts(account)
        if kind == 'invite' and label:
            rows = [row for row in rows if row.get('label', '') == label]
        contacts = {row['uid']: row['name'] for row in rows}
        globally_harvested = (self.store.harvested_ids() | self.store.ignored_ids()) if kind == 'collect' else set()
        users = (await self.collection_candidates(account, chat, amount,
                                                   set(contacts) | globally_harvested | {own})
                 if kind == 'collect' and amount else
                 await self.members(account, chat, fresh=kind == 'invite'))
        if kind == 'invite':
            for uid in self.store.confirmed_group_members(chat):
                users.setdefault(uid, '')
        if kind == 'collect':
            candidates = [(uid, name) for uid, name in users.items() if uid != own and uid not in contacts]
            excluded = len(users) - len(candidates)
        else:
            info = await self.request(account, self.clients[account].get_chat, chat)
            if str(getattr(info.type, 'value', info.type)) != 'CHAT':
                raise ValueError('Целевой чат должен быть группой. Приглашения в канал здесь не поддерживаются.')
            candidates = [(uid, name) for uid, name in contacts.items() if uid != own and uid not in users]
            excluded = len(contacts) - len(candidates)
        return {'kind': kind, 'chat': chat, 'candidates': candidates, 'excluded': excluded,
                'label': label}

    async def lookup(self, account, mode, value):
        client = self.clients[account]
        if mode == 'phone':
            value = re.sub(r'[\s()-]', '', value)
            if not re.fullmatch(r'\+[1-9]\d{7,14}', value):
                raise ValueError('Введите международный номер, например +79123456789')
            user = await self.request(account, client.search_by_phone, value)
        else:
            if not re.fullmatch(r'[1-9]\d{0,18}', value):
                raise ValueError('Введите положительный ID пользователя MAX')
            user = await self.request(account, client.get_user, int(value))
        if not user:
            raise ValueError('Участник не найден')
        return (user.id, user_name(user))

    async def resolve_list(self, account, entries):
        result = {}
        known = {row['uid'] for row in self.store.contacts(account)}
        for mode, value in entries:
            uid, name = await self.lookup(account, mode, value)
            if uid not in known:
                result[uid] = name
        return {'kind': 'collect', 'chat': 0, 'candidates': list(result.items()), 'excluded': len(entries) - len(result)}

    async def history(self, account, chat, count=30):
        messages = await self.request(account, self.clients[account].fetch_history, chat, backward=count)
        result = []
        for message in sorted(messages or [], key=lambda item: item.time, reverse=True):
            text = (message.text or '').replace('\n', ' ').strip()
            if not text:
                text = f'Вложения: {len(message.attaches or [])}' if message.attaches else 'Служебное сообщение'
            result.append((message.id, text[:180], message.time))
        return result

    async def forward_setup(self, account, chat):
        messages = await self.history(account, chat)
        folders = await self.request(account, self.clients[account].get_folders)
        return {'messages': messages,
                'folders': [(folder.title or 'Без названия', list(folder.include or []))
                            for folder in (folders.folders or [])]}

    async def run_forward_job(self, job, client):
        options = json.loads(job.get('options') or '{}')
        message_id = options.get('message_id')
        if not isinstance(message_id, int):
            raise ValueError('В задании не указан ID сообщения')
        source = job['chat']
        message = await self.request(job['account'], client.get_message, source, message_id)
        if not message:
            raise ValueError('Исходное сообщение не найдено. Ничего не отправлено.')
        states = self.store.items(job['id'])
        if 'pending' in states.values():
            raise ValueError('Есть неподтверждённая пересылка. Проверьте целевой чат перед повтором.')
        confirmed = sum(state == 'confirmed' for state in states.values())
        for target in json.loads(job['candidates']):
            self.checkpoint(job['id'])
            state = self.store.items(job['id']).get(target)
            if state and state != 'retryable':
                continue
            self.store.item(job['id'], target, 'pending', 'Пересылка отправляется', attempted=True)
            try:
                result = await self.request(job['account'], client.forward_message, target, message_id,
                                            source_chat_id=source)
            except ApiError as error:
                self.store.item(job['id'], target, 'retryable', safe_error(error))
                raise
            if not result:
                raise ValueError('MAX не подтвердил пересылку. Повтор запрещён до ручной проверки.')
            self.store.item(job['id'], target, 'confirmed', 'MAX подтвердил пересылку')
            confirmed += 1
            self.store.status(job['id'], 'running', f'Переслано {confirmed} / {job["amount"]}')
            self.event.emit('changed', job['account'])
        message = self.job_report(job)
        self.store.status(job['id'], 'complete', message)
        self.store.log(job['account'], message)
        return message

    async def run_invite_job(self, job, client, members, own_id):
        identity, account = job['id'], job['account']
        options = json.loads(job.get('options') or '{}')
        batch_size = 1 if options.get('force_single') else 20
        states = self.store.items(identity)
        confirmed = sum(state == 'confirmed' for state in states.values())
        candidates = []
        for uid in json.loads(job['candidates']):
            state = states.get(uid)
            if state and state != 'retryable':
                continue
            if uid == own_id or uid in members:
                self.store.item(identity, uid, 'skipped', 'Уже состоит в целевой группе или это текущий аккаунт')
                continue
            candidates.append(uid)
            if confirmed + len(candidates) >= job['amount']:
                break
        sent = 0
        cached_contacts = {contact.id: contact for contact in (client.contacts or []) if contact}
        for start in range(0, len(candidates), batch_size):
            self.checkpoint(identity)
            batch = candidates[start:start + batch_size]
            blocked = {uid for uid in batch
                       if uid in cached_contacts and blocked_user(cached_contacts[uid])}
            for uid in blocked:
                self.store.item(identity, uid, 'unavailable', 'Профиль недоступен или заблокирован')
                self.store.log(account, f'Участник ID {uid} пропущен: профиль недоступен или заблокирован')
            valid = [uid for uid in batch if uid not in blocked]
            if not valid:
                continue
            for uid in valid:
                self.store.item(identity, uid, 'pending', 'Приглашение отправляется', attempted=True)
            try:
                response = await self.request(account, client.invite_users_to_group,
                                              job['chat'], valid, show_history=False)
            except ApiError as error:
                privacy_filtered = (error.error or '').lower() == 'participants.filter.out'
                if privacy_filtered or (len(valid) == 1 and unavailable_contact(error)):
                    for uid in valid:
                        self.store.item(identity, uid, 'unavailable', safe_error(error))
                        self.store.log(account, f'Участник ID {uid} пропущен: {safe_error(error)}')
                    continue
                for uid in valid:
                    self.store.item(identity, uid, 'retryable', safe_error(error))
                raise
            response_members = chat_participant_ids(response)
            for uid in valid:
                if uid in response_members:
                    self.store.item(identity, uid, 'confirmed',
                                    'MAX подтвердил участника в ответе группы')
            sent += len(valid)
            self.store.status(identity, 'running', f'Пачка отправлена: {sent}; проверка после завершения')
            self.event.emit('changed', account)
        if sent and 'pending' in self.store.items(identity).values():
            refreshed = await self.members(account, job['chat'], fresh=True)
            for uid, state in self.store.items(identity).items():
                if state == 'pending' and uid in refreshed:
                    self.store.item(identity, uid, 'confirmed', 'Участник найден в целевой группе')
            if 'pending' in self.store.items(identity).values():
                raise ValueError('Часть приглашений не подтверждена в группе. Требуется ручная проверка.')
        message = self.job_report(job)
        self.store.status(identity, 'complete', message)
        self.store.log(account, message)
        return message

    @staticmethod
    def joined_chat(chat, own_id):
        status = str(getattr(chat, 'status', '')).upper()
        if status in ('LEFT', 'REMOVED', 'CLOSED', 'PENDING', 'REQUESTED'):
            return False
        return bool(chat and (own_id in chat_participant_ids(chat) or
                    (status == 'ACTIVE' and (getattr(chat, 'join_time', 0) or 0) > 0)))

    @staticmethod
    def invalid_group(error):
        code = (error.error or '').lower()
        if code in ('not.found', 'errors.not.found'):
            return error.opcode in (Opcode.CHAT_JOIN, Opcode.LINK_INFO)
        return code in {
            'link.invalid', 'invalid.link', 'link.expired', 'link.not.found',
            'chat.link.invalid', 'chat.link.expired', 'chat.not.found', 'chat.deleted',
            'chat.closed', 'chat.blocked', 'chat.full', 'chat.join.denied',
            'errors.link.invalid', 'errors.link.expired', 'errors.chat.not.found',
        }

    async def archive_dead_link(self, account, client, link, error):
        code = (error.error or '').lower()
        expired = {'link.invalid', 'invalid.link', 'link.expired', 'link.not.found',
                   'chat.link.invalid', 'chat.link.expired', 'errors.link.invalid', 'errors.link.expired'}
        missing = {'not.found', 'errors.not.found', 'chat.not.found', 'errors.chat.not.found', 'chat.deleted'}
        if code in expired or (error.opcode == Opcode.LINK_INFO and code in missing):
            self.store.invalidate_catalog_link(link, safe_error(error))
        elif error.opcode == Opcode.CHAT_JOIN and code in missing:
            # A join rejection alone need not mean the invitation URL is dead.
            try:
                await self.request(account, client.resolve_group_by_link, link)
            except ApiError as check:
                if check.opcode == Opcode.LINK_INFO and ((check.error or '').lower() in missing | expired):
                    self.store.invalidate_catalog_link(link, safe_error(check))
                else:
                    raise

    def skip_global_join(self, job, uid, record):
        reserved = record['state'] in ('reserved', 'pending')
        state = 'reserved_skip' if reserved else 'global_skip'
        text = 'Группа закреплена за заданием' if reserved else 'Группа уже есть в общем реестре'
        self.store.item(job, uid, state, f'{text}. Аккаунт: {record["account_name"]}')
        self.store.release_join_reservations(job, uid)

    async def run_join_job(self, job, client):
        from workspace_links import normalize_link
        identity, account = job['id'], job['account']
        own = client.me.contact.id
        rows = self.store.join_rows(identity)
        done = sum(row['state'] == 'confirmed' for row in rows)
        for row in rows:
            self.checkpoint(identity)
            if done >= job['amount']:
                break
            uid, link = row['uid'], normalize_link(row['link'])
            if row['state'] not in ('queued', 'retryable', 'pending'):
                continue
            if not link:
                self.store.item(identity, uid, 'unavailable', 'Некорректный формат ссылки')
                continue
            if row['state'] != 'pending' and self.store.catalog_inactive(link):
                self.store.item(identity, uid, 'unavailable', 'Ссылка ранее подтверждена как неактивная')
                continue
            previous = self.store.previous_join(account, link, exclude=(identity, uid))
            if previous and previous['state'] in ('confirmed', 'skipped') and row['state'] != 'pending':
                self.store.join_target_info(identity, uid, previous['chat'], previous['title'])
                self.store.item(identity, uid, 'skipped', 'Этот аккаунт уже вступал по ссылке')
                continue
            if row['state'] != 'pending' and not (previous and previous['state'] == 'pending'):
                claimed = self.store.claim_join(identity, uid, link)
                if claimed:
                    self.skip_global_join(identity, uid, claimed)
                    continue
            try:
                info = await self.request(account, client.resolve_group_by_link, link)
            except ApiError as error:
                if row['state'] != 'pending' and not (previous and previous['state'] == 'pending') and self.invalid_group(error):
                    self.store.item(identity, uid, 'unavailable', safe_error(error))
                    await self.archive_dead_link(account, client, link, error)
                    continue
                if row['state'] != 'pending':
                    self.store.item(identity, uid, 'retryable', safe_error(error))
                raise
            if row['state'] == 'pending' or (previous and previous['state'] == 'pending'):
                if not self.joined_chat(info, own):
                    raise ValueError('Предыдущее вступление не подтверждено. Проверьте группу или заявку в MAX. Повтор не отправлен.')
                if row['state'] == 'pending':
                    self.store.join_target_info(identity, uid, info.id, info.title or row['title'])
                    self.store.item(identity, uid, 'confirmed', 'Участие подтверждено повторной проверкой MAX')
                    done += 1
                    continue
                self.store.item(previous['job'], previous['uid'], 'confirmed', 'Участие подтверждено проверкой MAX')
            if not info:
                self.store.item(identity, uid, 'unavailable', 'MAX не вернул группу по ссылке')
                continue
            if str(getattr(info.type, 'value', info.type)) != 'CHAT':
                self.store.item(identity, uid, 'unavailable', 'Ссылка ведёт не в группу')
                continue
            self.store.join_target_info(identity, uid, info.id, info.title or row['title'])
            previous = self.store.previous_join(account, link, info.id, (identity, uid))
            if self.joined_chat(info, own):
                if previous and previous['state'] == 'pending':
                    self.store.item(previous['job'], previous['uid'], 'confirmed', 'Участие подтверждено проверкой MAX')
                self.store.item(identity, uid, 'skipped', 'Аккаунт уже состоит в группе')
                continue
            if previous:
                if previous['state'] == 'pending':
                    raise ValueError('Вступление в эту группу уже отправлялось по другой ссылке и не подтверждено. Повтор не отправлен.')
                self.store.item(identity, uid, 'skipped', 'Группа уже есть в истории этого аккаунта')
                continue
            self.checkpoint(identity)
            claimed = self.store.claim_join(identity, uid, link, info.id)
            if claimed:
                self.skip_global_join(identity, uid, claimed)
                continue
            # Persist before the write so crashes/timeouts never trigger an automatic resubmit.
            self.store.item(identity, uid, 'pending', 'Запрос вступления отправляется', attempted=True)
            try:
                result = await self.request(account, client.join_group, link)
            except ApiError as error:
                if self.invalid_group(error):
                    self.store.item(identity, uid, 'unavailable', safe_error(error))
                    await self.archive_dead_link(account, client, link, error)
                    continue
                code = (error.error or '').lower()
                if 'too-many' in code or 'flood' in code:
                    self.store.item(identity, uid, 'retryable', safe_error(error))
                else:
                    self.store.item(identity, uid, 'pending', safe_error(error))
                raise
            if not result or result.id != info.id or not self.joined_chat(result, own):
                self.store.item(identity, uid, 'pending', 'MAX не подтвердил участие; возможна заявка администратору')
                raise ValueError('Вступление не подтверждено. Возможно, требуется одобрение администратора. Повтор не отправлен.')
            self.store.item(identity, uid, 'confirmed', 'MAX подтвердил участие в группе')
            self.store.execute('INSERT OR REPLACE INTO chats VALUES(?,?,?,?)',
                               (account, info.id, result.title or info.title or row['title'], 'CHAT'))
            done += 1
            self.store.status(identity, 'running', f'Вступил {done} / {job["amount"]}')
            self.event.emit('changed', account)
        message = self.job_report(job)
        self.store.status(identity, 'complete', message)
        self.store.log(account, message)
        return message

    async def run_job(self, identity):
        job = self.store.job(identity)
        if job.get('deleted') or job['status'] in ('stopped', 'complete', 'running'):
            raise ValueError('Задание нельзя запустить в текущем состоянии')
        account = job['account']
        self.flags.pop(identity, None)
        initial = ('Проверка ссылок' if job['kind'] == 'join' else
                   'Проверка сообщения' if job['kind'] in ('forward', 'broadcast') else 'Проверка участников')
        self.store.status(identity, 'running', initial)
        self.event.emit('changed', account)
        self.metrics[account] = dict(job=identity, started=time.monotonic(), waiting=0., network=0., requests=0, cached_pages=0)
        try:
            status = self.store.rows('SELECT connection_state FROM accounts WHERE id=?', (account,))[0]['connection_state']
            if status in ('needs_login', 'blocked') or not self.connected(account):
                raise JobPaused('Аккаунт недоступен. Проверьте статус и подключение.')
            client = self.clients[account]
            blocked = self.store.rows('SELECT blocked_until FROM accounts WHERE id=?', (account,))[0]['blocked_until']
            if blocked > time.time():
                raise JobPaused('Локальная пауза после ограничения MAX ещё не закончилась')
            if job['kind'] == 'broadcast':
                from workspace_broadcast import run_broadcast
                return await run_broadcast(self, job, client)
            own_id = client.me.contact.id
            if job['kind'] == 'join':
                return await self.run_join_job(job, client)
            if job['kind'] == 'forward':
                return await self.run_forward_job(job, client)
            if job['kind'] == 'invite':
                info = await self.request(account, client.get_chat, job['chat'])
                if str(getattr(info.type, 'value', info.type)) != 'CHAT':
                    raise ValueError('Целевой чат должен быть группой')
            users = await self.members(account, job['chat'], fresh=True, job=identity) if job['kind'] == 'invite' else {}
            if job['kind'] == 'invite':
                for uid in self.store.confirmed_group_members(job['chat']):
                    users.setdefault(uid, '')
            known = {row['uid'] for row in self.store.contacts(account)}
            states = self.store.items(identity)
            for uid, state in states.items():
                if state == 'pending':
                    confirmed = uid in users if job['kind'] == 'invite' else uid in {contact.id for contact in (client.contacts or []) if contact}
                    if not confirmed:
                        raise ValueError('Есть неподтверждённая операция. Проверьте результат в MAX; повторная отправка запрещена.')
                    self.store.item(identity, uid, 'confirmed', 'Результат подтверждён при возобновлении')
                    if job['kind'] == 'collect':
                        self.store.contact(account, uid, users.get(uid, ''), str(job['chat'] or 'Вручную'))
                        self.store.confirm_harvest(job['chat'], uid, account, identity)
            if job['kind'] == 'invite':
                return await self.run_invite_job(job, client, users, own_id)
            states = self.store.items(identity)
            options = json.loads(job.get('options') or '{}')
            confirmed = sum(state == 'confirmed' for state in states.values())
            async for uid in self.collection_queue(job, known, own_id):
                self.checkpoint(identity)
                if confirmed >= job['amount']:
                    break
                if uid in states and states[uid] != 'retryable':
                    continue
                if uid == own_id or uid in known:
                    self.store.item(identity, uid, 'skipped', 'Контакт уже существует или это текущий аккаунт')
                    states[uid] = 'skipped'
                    continue
                if self.store.is_ignored(uid):
                    self.store.item(identity, uid, 'unavailable', 'Профиль в игноре')
                    states[uid] = 'unavailable'
                    continue
                claim = self.store.claim_harvest(job['chat'], uid, account, identity)
                if claim in ('confirmed', 'pending'):
                    detail = ('Уже был взят другим аккаунтом или заданием' if claim == 'confirmed'
                              else 'Зарезервирован незавершённым заданием другого аккаунта')
                    self.store.item(identity, uid, 'skipped', detail)
                    states[uid] = 'skipped'
                    continue
                ambiguous = self.store.rows('SELECT i.uid FROM items i JOIN jobs j ON j.id=i.job WHERE j.account=? AND j.kind=? AND (j.kind=\'collect\' OR j.chat=?) AND i.uid=? AND i.state=\'pending\' AND j.id<>?', (account, job['kind'], job['chat'], uid, identity))
                if ambiguous:
                    self.store.release_harvest(job['chat'], uid, identity)
                    raise ValueError('Этот участник имеет неподтверждённую операцию в другом задании. Проверьте MAX перед повтором.')
                self.store.item(identity, uid, 'pending', 'Добавление контакта отправляется', attempted=True)
                try:
                    result = await self.request(account, client.add_contact, uid)
                except ApiError as error:
                    if unavailable_contact(error):
                        if (error.error or '').lower() in {'user.blocked', 'contact.blocked', 'errors.user.blocked', 'errors.contact.blocked', 'user.deleted', 'contact.deleted'}:
                            self.store.ignore(uid, safe_error(error))
                        self.store.item(identity, uid, 'unavailable', safe_error(error))
                        states[uid] = 'unavailable'
                        self.store.release_harvest(job['chat'], uid, identity)
                        self.store.log(account, f'Участник ID {uid} пропущен: {safe_error(error)}')
                        self.store.status(identity, 'running', 'Недоступный участник пропущен; продолжаем')
                        self.event.emit('changed', account)
                        continue
                    self.store.item(identity, uid, 'retryable', safe_error(error))
                    self.store.release_harvest(job['chat'], uid, identity)
                    raise
                if not result or result.id != uid:
                    raise ValueError('MAX не подтвердил контакт. Задание остановлено без повторного запроса.')
                self.store.confirm_collection(job, uid, user_name(result), options.get('label', ''), confirmed + 1)
                states[uid] = 'confirmed'
                known.add(uid)
                confirmed += 1
                self.event.emit('changed', account)
                if confirmed >= job['amount']:
                    break
            message = self.job_report(job)
            self.store.status(identity, 'complete', message)
            self.store.log(account, message)
            return message
        except Exception as error:
            state = 'paused' if isinstance(error, JobPaused) else 'stopped' if isinstance(error, JobStopped) else 'needs_review'
            if isinstance(error, ApiError) and ('too-many' in (error.error or '').lower() or 'flood' in (error.error or '').lower()):
                state = 'limited'
            self.store.status(identity, state, safe_error(error))
            raise
        except asyncio.CancelledError:
            self.store.status(identity, 'interrupted', 'Приложение закрыто; требуется проверка и ручное возобновление')
            raise
        finally:
            if job['kind'] == 'join':
                self.store.release_join_reservations(identity)
            metrics = self.metrics.pop(account, None)
            if metrics:
                metrics['elapsed'] = time.monotonic() - metrics['started']
                self.store.save_metrics(identity, metrics)

    async def collection_queue(self, job, known, own):
        seen = set(self.store.items(job['id']))
        for uid in json.loads(job['candidates']):
            seen.add(uid)
            yield uid
        options = json.loads(job.get('options') or '{}')
        if not job['chat'] or not options.get('refill'):
            return
        if sum(s == 'confirmed' for s in self.store.items(job['id']).values()) >= job['amount']:
            return
        async for batch in self.collection_pages(job['account'], job['chat'], job['id']):
            self.checkpoint(job['id'])
            if sum(s == 'confirmed' for s in self.store.items(job['id']).values()) >= job['amount']:
                return
            excluded = seen | known | self.store.harvested_ids() | self.store.ignored_ids() | {own}
            for uid, name, blocked in batch:
                if uid in excluded:
                    continue
                seen.add(uid)
                self.store.append_candidate(job['id'], uid)
                if blocked:
                    self.store.ignore(uid, 'Профиль заблокирован или удалён')
                    self.store.item(job['id'], uid, 'unavailable', 'Профиль заблокирован или удалён')
                    continue
                yield uid

    async def shutdown(self):
        operations = [asyncio.wrap_future(future) for future in self.futures.values() if not future.done()]
        for future in self.futures.values():
            if not future.done():
                future.cancel()
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)
        for account in list(self.clients):
            try:
                await self.disconnect(account)
            except Exception as error:
                self.store.log(account, safe_error(error))
