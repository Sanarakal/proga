"""Broadcast runner using durable per-recipient intent and MAX payload models."""
import asyncio
import time
from datetime import datetime, timedelta

from pymax.exceptions import ApiError
from pymax.files import Photo, Video
from pymax.protocol import Opcode
from pymax.api.messages.payloads import SendMessagePayload, SendMessagePayloadMessage
from pymax.api.response import require_payload_model
from pymax.types import Message
from pymax.types.domain.element import Element

from workspace_posts import next_window, validate_content


class PostTransport:
    def __init__(self, client):
        self.client = client

    async def upload_photo(self, data, name):
        return await self.client._app.api.uploads.upload_photo(Photo(raw=data, name=name))

    async def upload_video(self, data, name):
        return await self.client._app.api.uploads.upload_video(Video(raw=data, name=name))

    async def send_post(self, target, content, attachments, cid, notify):
        elements = []
        for entity in content['elements']:
            raw = dict(entity)
            raw['from_'] = raw.pop('from')
            if raw['type'] == 'LINK':
                raw['attributes'] = {**raw.get('attributes', {}), 'url': raw.pop('url')}
            elements.append(Element.model_validate(raw))
        payload = SendMessagePayload(chat_id=target, notify=notify,
            message=SendMessagePayloadMessage(text=content['text'] or None, cid=cid,
                                              elements=elements, attaches=attachments))
        # One MSG_SEND invocation, without the library's automatic attachment retry.
        response = await self.client._app.invoke(Opcode.MSG_SEND, payload.to_payload())
        return require_payload_model(response, Message)


def broadcast_report(store, identity):
    post = store.broadcast(identity)
    rows = store.broadcast_rows(identity)
    counts = {state: sum(r['state'] == state for r in rows)
              for state in ('confirmed', 'pending', 'queued', 'retryable', 'skipped', 'unavailable')}
    return '\n'.join((post['title'], f"Отправлено: {counts['confirmed']} / {len(rows)}",
                      f"Ожидает: {counts['queued']}; отказ MAX: {counts['retryable']}",
                      f"Пропущено: {counts['skipped']}; нет доступа: {counts['unavailable']}",
                      f"Нужна проверка: {counts['pending']}"))


async def wait_for_slot(engine, identity, timestamp):
    from workspace_engine import JobPaused
    last = time.time()
    while True:
        engine.checkpoint(identity)
        now = time.time()
        if now - last > 15 or now < last - 2:
            raise JobPaused('Сон ноутбука или изменение часов. Продолжение вручную.')
        if now >= timestamp:
            return
        await asyncio.sleep(min(0.25, timestamp - now))
        last = now


async def run_broadcast(engine, job, client):
    from workspace_engine import JobPaused, safe_error
    store, identity, account = engine.store, job['id'], job['account']
    broadcast = store.broadcast(identity)
    content = validate_content(broadcast['content'])
    options = broadcast['settings']
    rows = store.broadcast_rows(identity)
    if any(row['state'] == 'pending' for row in rows):
        raise ValueError('Есть неподтверждённая отправка. Откройте отчёт и выполните ручную проверку.')
    transport = PostTransport(client)
    attachments = None
    due = broadcast['next_at']
    previous = [row for row in rows if row['state'] != 'queued']
    if previous:
        due = max(due, time.time() + options['interval'])
    for row in rows:
        if row['state'] not in ('queued', 'retryable'):
            continue
        engine.checkpoint(identity)
        due = next_window(max(time.time(), due), options['window_start'], options['window_end'])
        if options['daily_limit']:
            day = datetime.fromtimestamp(due).replace(hour=0, minute=0, second=0, microsecond=0)
            tomorrow = day + timedelta(days=1)
            count = store.rows('''SELECT COUNT(*) AS n FROM broadcast_attempts
                WHERE account=? AND at>=? AND at<?''', (account, day.timestamp(), tomorrow.timestamp()))[0]['n']
            if count >= options['daily_limit']:
                due = next_window(tomorrow.timestamp(), options['window_start'], options['window_end'])
        if options['end_at'] and due >= options['end_at']:
            store.status(identity, 'complete', 'Срок рассылки завершён.\n' + broadcast_report(store, identity))
            return 'Срок рассылки завершён.'
        store.execute('UPDATE broadcasts SET next_at=? WHERE job=?', (due, identity))
        when = datetime.fromtimestamp(due).strftime('%d.%m %H:%M:%S')
        store.status(identity, 'running', f"Проход {row['round']}: {row['name']} / отправка {when}")
        engine.event.emit('changed', account)
        await wait_for_slot(engine, identity, due)
        if not engine.available(account):
            raise JobPaused('Аккаунт недоступен. Проверьте подключение и продолжите вручную.')
        if attachments is None:
            attachments = []
            for photo_id in content['photos']:
                engine.checkpoint(identity)
                media = store.rows('SELECT data,name,kind FROM post_media WHERE id=?', (photo_id,))
                if not media:
                    raise ValueError('Вложение поста отсутствует.')
                if media[0]['kind'] == 'VIDEO':
                    upload = await engine.request(account, transport.upload_video, media[0]['data'], media[0]['name'], _timeout=300)
                else:
                    upload = await engine.request(account, transport.upload_photo, media[0]['data'], media[0]['name'])
                if not upload:
                    raise ValueError('MAX не подтвердил загрузку вложения. Сообщение не отправлено.')
                attachments.append(upload)

        async def send_post():
            engine.checkpoint(identity)
            now = time.time()
            if (options['end_at'] and now >= options['end_at']) or next_window(now, options['window_start'], options['window_end']) > now:
                raise JobPaused('Рабочее время закончилось до отправки. Продолжение вручную.')
            store.begin_delivery(account, identity, row['seq'])
            return await transport.send_post(row['chat'], content, attachments, row['cid'], options['notify'])

        state, detail, message_id = 'confirmed', 'MAX подтвердил отправку', None
        try:
            result = await engine.request(account, send_post)
            message_id = getattr(result, 'id', None)
            returned_chat = getattr(result, 'chat_id', None)
            returned_cid = getattr(result, 'cid', None)
            if not message_id or (returned_chat is not None and returned_chat != row['chat']) or (returned_cid is not None and returned_cid != row['cid']):
                raise ValueError('Ответ MAX не подтверждает отправку в выбранный чат. Нужна ручная проверка.')
        except ApiError as error:
            code = (error.error or '').lower().removeprefix('errors.')
            detail = safe_error(error)
            denied = code in {'chat.denied', 'chat.write.denied', 'chat.access.denied', 'chat.not.member', 'chat.not.found'}
            if denied and options['skip_denied']:
                state = 'unavailable'
            elif denied or code in {'attachment.not.ready', 'session.revoked', 'session.expired', 'account.blocked', 'account.banned'} or 'flood' in code or 'too-many' in code:
                store.finish_delivery(identity, row['seq'], 'retryable', detail)
                raise
            else:
                # Unknown failures are not sufficient evidence that no message was sent.
                raise
        now = time.time()
        due = now + options['interval']
        if options['batch'] and row['seq'] % options['batch'] == 0:
            due = max(due, now + options['batch_pause'])
        next_row = next((item for item in rows if item['seq'] == row['seq'] + 1), None)
        if next_row and next_row['round'] != row['round']:
            due = max(due, now + options['repeat_seconds'])
        store.finish_delivery(identity, row['seq'], state, detail, str(message_id) if message_id else None, due)
        engine.event.emit('changed', account)
    report = broadcast_report(store, identity)
    store.status(identity, 'complete', report)
    store.log(account, report)
    return report
