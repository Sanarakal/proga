"""Persistent post snapshots and bounded broadcast plans. No network access."""
import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from urllib.parse import urlsplit

from PIL import Image, ImageOps


MAX_TEXT = 4000
MAX_PHOTOS = 10
MAX_MEDIA_BYTES = 50 * 1024 * 1024
MAX_TARGETS = 1000
MAX_DELIVERIES = 10000
STATES = {'queued': 'Ожидает', 'pending': 'Нужна проверка', 'confirmed': 'Отправлено',
          'retryable': 'Отказ MAX', 'skipped': 'Пропущено', 'unavailable': 'Нет доступа'}


def link_url(value):
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname or any(ord(c) < 32 for c in value):
        raise ValueError('Укажите полный адрес ссылки: https://...')
    return value


def validate_content(content, ready=True):
    text = content.get('text', '')
    photos = content.get('photos', [])
    if not isinstance(text, str) or len(text.encode('utf-16-le')) // 2 > MAX_TEXT:
        raise ValueError(f'Пост: не более {MAX_TEXT} символов UTF-16.')
    if len(photos) > MAX_PHOTOS:
        raise ValueError(f'Добавьте до {MAX_PHOTOS} вложений.')
    if ready and not text.strip() and not photos:
        raise ValueError('Добавьте текст, фото или видео.')
    raw = text.encode('utf-16-le')
    elements = []
    for item in content.get('elements', []):
        kind, start, length = item['type'], item['from'], item['length']
        if not isinstance(kind, str) or not kind or len(kind) > 100 or not isinstance(start, int) or not isinstance(length, int) or start < 0 or length <= 0 or (start + length) * 2 > len(raw):
            raise ValueError('Некорректное форматирование поста.')
        # Verify boundaries do not cut an emoji's UTF-16 surrogate pair.
        raw[:start * 2].decode('utf-16-le')
        raw[start * 2:(start + length) * 2].decode('utf-16-le')
        element = json.loads(json.dumps(item, ensure_ascii=False))
        if kind == 'LINK':
            element['url'] = link_url(item.get('url') or item.get('attributes', {}).get('url', ''))
        elements.append(element)
    return dict(text=text, elements=elements, photos=list(photos))


def validate_options(options):
    defaults = dict(interval=30, batch=0, batch_pause=300, rounds=1, repeat_seconds=86400,
                    start_at=0, end_at=0, window_start=0, window_end=0, daily_limit=0,
                    skip_denied=True, notify=True)
    result = {key: options.get(key, value) for key, value in defaults.items()}
    for key, low, high in (('interval', 1, 86400), ('batch', 0, 1000), ('batch_pause', 1, 86400),
                           ('rounds', 1, 100), ('repeat_seconds', 60, 31536000),
                           ('window_start', 0, 1439), ('window_end', 0, 1439), ('daily_limit', 0, 10000)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Некорректная настройка: ' + key)
    for key in ('start_at', 'end_at'):
        result[key] = float(result[key])
        if not 0 <= result[key] <= 32503680000:
            raise ValueError('Некорректная дата.')
    if result['end_at'] and result['end_at'] <= max(time.time(), result['start_at']):
        raise ValueError('Окончание должно быть позже начала рассылки.')
    return result


def next_window(timestamp, start, end):
    if start == end:
        return timestamp
    now = datetime.fromtimestamp(timestamp)
    minute = now.hour * 60 + now.minute
    if (start < end and start <= minute < end) or (start > end and (minute >= start or minute < end)):
        return timestamp
    target = now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


class PostStore:
    def init_posts(self):
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY,title TEXT NOT NULL,
                    content TEXT NOT NULL,ready INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS post_media(id TEXT PRIMARY KEY,data BLOB NOT NULL,
                    name TEXT NOT NULL,thumbnail BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS broadcasts(job TEXT PRIMARY KEY,title TEXT NOT NULL,
                    content TEXT NOT NULL,settings TEXT NOT NULL,next_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS broadcast_targets(job TEXT NOT NULL,seq INTEGER NOT NULL,
                    chat INTEGER NOT NULL,name TEXT NOT NULL,round INTEGER NOT NULL,cid INTEGER NOT NULL,
                    message_id TEXT,PRIMARY KEY(job,seq));
                CREATE TABLE IF NOT EXISTS broadcast_sets(account TEXT NOT NULL,name TEXT NOT NULL,
                    chats TEXT NOT NULL,PRIMARY KEY(account,name));
                CREATE INDEX IF NOT EXISTS broadcast_target_chat ON broadcast_targets(chat);
                CREATE TABLE IF NOT EXISTS broadcast_attempts(id INTEGER PRIMARY KEY,
                    account TEXT NOT NULL,job TEXT NOT NULL,seq INTEGER NOT NULL,at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS broadcast_attempt_day ON broadcast_attempts(account,at);
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(post_media)')}
            if 'kind' not in columns:
                db.execute("ALTER TABLE post_media ADD COLUMN kind TEXT NOT NULL DEFAULT 'PHOTO'")

    def add_post_photo(self, path):
        from pathlib import Path
        path = Path(path)
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError('Фотография должна быть не больше 10 МБ.')
        raw = path.read_bytes()
        with Image.open(BytesIO(raw)) as source:
            if source.width * source.height > 40_000_000:
                raise ValueError('Изображение слишком большое: максимум 40 мегапикселей.')
            source.seek(0)
            photo = ImageOps.exif_transpose(source).convert('RGB')
            photo.thumbnail((4096, 4096))
            full = BytesIO()
            photo.save(full, 'JPEG', quality=92)
            photo.thumbnail((240, 160))
            thumb = BytesIO()
            photo.save(thumb, 'JPEG', quality=85)
        data = full.getvalue()
        identity = hashlib.sha256(data).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM post_media WHERE id=?', (identity,)).fetchone():
                total = db.execute('SELECT COALESCE(SUM(length(data)),0) FROM post_media').fetchone()[0]
                if total + len(data) > 128 * 1024 * 1024:
                    raise ValueError('Хранилище фото заполнено (128 МБ). Удалите ненужные посты.')
                db.execute('INSERT INTO post_media(id,data,name,thumbnail) VALUES(?,?,?,?)',
                           (identity, data, identity + '.jpg', thumb.getvalue()))
        return identity

    @staticmethod
    def prepare_import_media(data, kind):
        if not data or len(data) > MAX_MEDIA_BYTES:
            raise ValueError('Вложение пустое или превышает 50 МБ.')
        if kind == 'PHOTO':
            with Image.open(BytesIO(data)) as source:
                if source.width * source.height > 40_000_000:
                    raise ValueError('Изображение превышает 40 мегапикселей.')
                extension = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp', 'GIF': '.gif', 'BMP': '.bmp'}.get(source.format)
                if not extension:
                    raise ValueError('Формат фотографии не поддерживается.')
                image = ImageOps.exif_transpose(source).convert('RGB')
                image.thumbnail((240, 160))
                buffer = BytesIO()
                image.save(buffer, 'JPEG', quality=85)
                thumbnail = buffer.getvalue()
        elif kind == 'VIDEO':
            if len(data) < 12 or data[4:8] != b'ftyp':
                raise ValueError('MAX не вернул поддерживаемый файл MP4.')
            extension, thumbnail = '.mp4', b''
        else:
            raise ValueError('Тип вложения не поддерживается: ' + kind)
        identity = hashlib.sha256(kind.encode('ascii') + data).hexdigest()
        return dict(id=identity, data=data, name=identity + extension, thumbnail=thumbnail, kind=kind)

    def save_imported_post(self, title, content, media):
        content = validate_content(content)
        identity = uuid.uuid4().hex
        unique = {item['id']: item for item in media}
        if set(content['photos']) != set(unique):
            raise ValueError('Импорт не завершён: не все вложения загружены.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = {row[0] for row in db.execute('SELECT id FROM post_media')}
            total = db.execute('SELECT COALESCE(SUM(length(data)),0) FROM post_media').fetchone()[0]
            if total + sum(len(item['data']) for key, item in unique.items() if key not in existing) > 128 * 1024 * 1024:
                raise ValueError('Вложения не помещаются в локальное хранилище (128 МБ). Пост не импортирован.')
            for item in unique.values():
                db.execute('INSERT OR IGNORE INTO post_media(id,data,name,thumbnail,kind) VALUES(?,?,?,?,?)',
                           (item['id'], item['data'], item['name'], item['thumbnail'], item['kind']))
            db.execute('INSERT INTO posts VALUES(?,?,?,?,?)',
                       (identity, title.strip()[:100] or 'Пост из MAX', json.dumps(content, ensure_ascii=False), 1, time.time()))
        return identity

    def post(self, identity):
        rows = self.rows('SELECT * FROM posts WHERE id=?', (identity,))
        if not rows:
            raise ValueError('Пост не найден.')
        row = rows[0]
        row['content'] = json.loads(row['content'])
        return row

    def save_post(self, identity, title, content, ready=False):
        content = validate_content(content, ready)
        identity = identity or uuid.uuid4().hex
        title = title.strip()[:100] or 'Без названия'
        with self.db() as db:
            for photo in content['photos']:
                if not db.execute('SELECT 1 FROM post_media WHERE id=?', (photo,)).fetchone():
                    raise ValueError('Фотография не найдена.')
            db.execute('''INSERT INTO posts VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,content=excluded.content,ready=excluded.ready,updated=excluded.updated''',
                       (identity, title, json.dumps(content, ensure_ascii=False), int(ready), time.time()))
        return identity

    def delete_post(self, identity):
        self.execute('DELETE FROM posts WHERE id=?', (identity,))
        self.prune_post_media()

    def prune_post_media(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            used = set()
            for table in ('posts', 'broadcasts'):
                for row in db.execute(f'SELECT content FROM {table}'):
                    used.update(json.loads(row[0])['photos'])
            for row in db.execute('SELECT id FROM post_media').fetchall():
                if row[0] not in used:
                    db.execute('DELETE FROM post_media WHERE id=?', (row[0],))

    def new_broadcast(self, post, account, targets, title, options):
        content = validate_content(self.post(post)['content'])
        settings = validate_options(options)
        targets = list(dict.fromkeys(targets))
        if not 1 <= len(targets) <= MAX_TARGETS or len(targets) * settings['rounds'] > MAX_DELIVERIES:
            raise ValueError('Не более 1000 чатов и 10000 отправок в одном задании.')
        identity = uuid.uuid4().hex
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM accounts WHERE id=?', (account,)).fetchone():
                raise ValueError('Выберите аккаунт.')
            for photo in content['photos']:
                if not db.execute('SELECT 1 FROM post_media WHERE id=?', (photo,)).fetchone():
                    raise ValueError('Фотография поста отсутствует.')
            chats = dict(db.execute("SELECT uid,name FROM chats WHERE account=? AND kind IN ('CHAT','CHANNEL','DIALOG')", (account,)).fetchall())
            if any(chat not in chats for chat in targets):
                raise ValueError('Выберите чаты выбранного аккаунта. Обновите список чатов.')
            count = len(targets) * settings['rounds']
            db.execute('''INSERT INTO jobs(id,account,kind,chat,amount,candidates,status,message,created,options)
                VALUES(?,?,'broadcast',0,?,?,'queued','',?,'{}')''',
                       (identity, account, count, json.dumps(list(range(1, count + 1))), time.time()))
            db.execute('INSERT INTO broadcasts VALUES(?,?,?,?,?)',
                       (identity, title.strip()[:100] or 'Рассылка', json.dumps(content, ensure_ascii=False),
                        json.dumps(settings), settings['start_at']))
            for cycle in range(1, settings['rounds'] + 1):
                for index, chat in enumerate(targets):
                    seq = (cycle - 1) * len(targets) + index + 1
                    cid = uuid.uuid4().int % (2**52) + 1
                    db.execute('INSERT INTO broadcast_targets VALUES(?,?,?,?,?,?,NULL)',
                               (identity, seq, chat, chats[chat], cycle, cid))
        return identity

    def broadcast(self, identity):
        row = self.rows('SELECT * FROM broadcasts WHERE job=?', (identity,))[0]
        row['content'] = json.loads(row['content'])
        row['settings'] = json.loads(row['settings'])
        return row

    def broadcast_rows(self, identity):
        return self.rows('''SELECT t.*,COALESCE(i.state,'queued') AS state,
            COALESCE(i.detail,'') AS detail,COALESCE(i.updated,0) AS updated,
            COALESCE(i.attempts,0) AS attempts FROM broadcast_targets t
            LEFT JOIN items i ON i.job=t.job AND i.uid=t.seq WHERE t.job=? ORDER BY t.seq''', (identity,))

    def finish_delivery(self, job, seq, state, detail, message_id=None, next_at=0):
        with self.db() as db:
            db.execute('''UPDATE items SET state=?,detail=?,updated=? WHERE job=? AND uid=?''',
                       (state, detail, time.time(), job, seq))
            db.execute('UPDATE broadcast_targets SET message_id=? WHERE job=? AND seq=?', (message_id, job, seq))
            db.execute('UPDATE broadcasts SET next_at=? WHERE job=?', (next_at, job))

    def begin_delivery(self, account, job, seq):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT state FROM items WHERE job=? AND uid=?', (job, seq)).fetchone()
            if current and current[0] not in ('queued', 'retryable'):
                raise ValueError('Эта отправка уже выполнялась; повтор запрещён.')
            now = time.time()
            db.execute('''INSERT INTO items(job,uid,state,detail,attempts,updated)
                VALUES(?,?,'pending','Ожидается ответ MAX',1,?) ON CONFLICT(job,uid)
                DO UPDATE SET state='pending',detail=excluded.detail,attempts=items.attempts+1,updated=excluded.updated''', (job, seq, now))
            db.execute('INSERT INTO broadcast_attempts(account,job,seq,at) VALUES(?,?,?,?)', (account, job, seq, now))

    def resolve_delivery(self, job, seq, state):
        if state not in ('confirmed', 'skipped'):
            raise ValueError('Выберите подтверждение или пропуск без повтора.')
        with self.db() as db:
            current = db.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()
            if not current or current[0] == 'running':
                raise ValueError('Сначала остановите рассылку.')
            db.execute("UPDATE items SET state=?,detail='Ручная проверка пользователем',updated=? WHERE job=? AND uid=? AND state='pending'",
                       (state, time.time(), job, seq))
