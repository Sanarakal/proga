import json
import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from workspace_posts import PostStore


class Store(PostStore):
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'workspace.sqlite'
        self.init_posts()
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    max_id INTEGER, blocked_until REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS contacts(account TEXT, uid INTEGER, name TEXT,
                    source TEXT, PRIMARY KEY(account,uid));
                CREATE TABLE IF NOT EXISTS chats(account TEXT, uid INTEGER, name TEXT, kind TEXT,
                    PRIMARY KEY(account,uid));
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, account TEXT, kind TEXT,
                    chat INTEGER, amount INTEGER, candidates TEXT, status TEXT, message TEXT,
                    created REAL);
                CREATE TABLE IF NOT EXISTS items(job TEXT, uid INTEGER, state TEXT,
                    PRIMARY KEY(job,uid));
                CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY, at REAL,
                    account TEXT, message TEXT);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS sources(chat INTEGER PRIMARY KEY, title TEXT,
                    total INTEGER DEFAULT 0, updated REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS harvested(source INTEGER, uid INTEGER, account TEXT,
                    job TEXT, added REAL, PRIMARY KEY(source,uid));
                CREATE TABLE IF NOT EXISTS harvest_claims(source INTEGER, uid INTEGER,
                    account TEXT, job TEXT, created REAL, PRIMARY KEY(source,uid));
            ''')
            contact_columns = {row[1] for row in db.execute('PRAGMA table_info(contacts)')}
            if 'label' not in contact_columns:
                db.execute("ALTER TABLE contacts ADD COLUMN label TEXT DEFAULT ''")
            job_columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            if 'deleted' not in job_columns:
                db.execute('ALTER TABLE jobs ADD COLUMN deleted INTEGER DEFAULT 0')
            db.execute('CREATE TABLE IF NOT EXISTS ignored(uid INTEGER PRIMARY KEY, reason TEXT, added REAL)')
            db.execute('''CREATE TABLE IF NOT EXISTS join_targets(job TEXT, uid INTEGER,
                link TEXT NOT NULL, title TEXT, source TEXT, chat INTEGER,
                PRIMARY KEY(job,uid), UNIQUE(job,link))''')
            db.execute('CREATE INDEX IF NOT EXISTS join_link ON join_targets(link)')
            db.execute('CREATE INDEX IF NOT EXISTS join_chat ON join_targets(chat)')
            db.execute('''CREATE TABLE IF NOT EXISTS join_registry(key TEXT PRIMARY KEY,
                account TEXT, account_name TEXT, job TEXT, uid INTEGER, state TEXT, title TEXT, updated REAL)''')
            db.execute('CREATE INDEX IF NOT EXISTS join_registry_job ON join_registry(job,uid)')
            db.execute('''CREATE TABLE IF NOT EXISTS join_files(id TEXT PRIMARY KEY, name TEXT,
                filename TEXT, digest TEXT UNIQUE, original BLOB, data TEXT, created REAL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS chat_catalog(link TEXT PRIMARY KEY, title TEXT,
                chat INTEGER, state TEXT NOT NULL DEFAULT 'active', reason TEXT DEFAULT '',
                import_name TEXT, added REAL, updated REAL)''')
            db.execute('CREATE INDEX IF NOT EXISTS catalog_chat ON chat_catalog(chat,state)')
            db.execute('CREATE TABLE IF NOT EXISTS chat_folders(id TEXT PRIMARY KEY,name TEXT NOT NULL)')
            db.execute("INSERT OR IGNORE INTO chat_folders VALUES('all','Все чаты')")
            db.execute('''CREATE TABLE IF NOT EXISTS chat_folder_links(folder TEXT,link TEXT,
                PRIMARY KEY(folder,link))''')
            db.execute('''CREATE TABLE IF NOT EXISTS source_pages(account TEXT, chat INTEGER, marker INTEGER,
                members TEXT, next_marker INTEGER, created REAL, PRIMARY KEY(account,chat,marker))''')
            db.execute('''CREATE TABLE IF NOT EXISTS job_metrics(job TEXT PRIMARY KEY, elapsed REAL,
                waiting REAL, network REAL, requests INTEGER, cached_pages INTEGER)''')
            db.execute('CREATE TABLE IF NOT EXISTS collection_templates(name TEXT PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS favorite_chats(account TEXT, chat INTEGER, PRIMARY KEY(account,chat))')
            db.execute('CREATE TABLE IF NOT EXISTS chat_roles(account TEXT, chat INTEGER, admin INTEGER, PRIMARY KEY(account,chat))')
            if 'options' not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN options TEXT DEFAULT '{}'")
            account_columns = {row[1] for row in db.execute('PRAGMA table_info(accounts)')}
            for name, definition in (
                ('last_connected', 'REAL DEFAULT 0'),
                ('last_error', "TEXT DEFAULT ''"),
                ('chats_synced', 'REAL DEFAULT 0'),
                ('connection_state', "TEXT DEFAULT 'offline'"),
                ('status_detail', "TEXT DEFAULT ''"),
                ('status_changed', 'REAL DEFAULT 0'),
                ('status_checked', 'REAL DEFAULT 0'),
            ):
                if name not in account_columns:
                    db.execute(f'ALTER TABLE accounts ADD COLUMN {name} {definition}')
            if 'connection_state' not in account_columns:
                db.execute("UPDATE accounts SET connection_state='needs_login' WHERE max_id IS NULL")
            db.execute("""UPDATE accounts SET connection_state='offline',
                status_detail='Приложение перезапущено. Подключите аккаунт.', status_changed=?
                WHERE connection_state IN ('online','connecting')""", (time.time(),))
            item_columns = {row[1] for row in db.execute('PRAGMA table_info(items)')}
            for name, definition in (
                ('detail', "TEXT DEFAULT ''"),
                ('attempts', 'INTEGER DEFAULT 0'),
                ('updated', 'REAL DEFAULT 0'),
            ):
                if name not in item_columns:
                    db.execute(f'ALTER TABLE items ADD COLUMN {name} {definition}')
            db.execute('''INSERT OR IGNORE INTO sources(chat,title,total,updated)
                SELECT j.chat, COALESCE(MAX(c.name),'Группа ' || j.chat), 0, MAX(j.created)
                FROM jobs j LEFT JOIN chats c ON c.account=j.account AND c.uid=j.chat
                WHERE j.kind='collect' AND j.chat<>0 GROUP BY j.chat''')
            db.execute('''INSERT OR IGNORE INTO harvested(source,uid,account,job,added)
                SELECT j.chat,i.uid,j.account,j.id,
                    CASE WHEN i.updated>0 THEN i.updated ELSE j.created END
                FROM items i JOIN jobs j ON j.id=i.job
                WHERE j.kind='collect' AND j.chat<>0 AND i.state='confirmed'
                ORDER BY CASE WHEN i.updated>0 THEN i.updated ELSE j.created END''')
            # Keep the earliest confirmed source for each MAX user across all accounts.
            db.execute('''DELETE FROM harvested WHERE rowid NOT IN (
                SELECT rowid FROM harvested first
                WHERE rowid=(SELECT rowid FROM harvested candidate
                    WHERE candidate.uid=first.uid ORDER BY candidate.added,candidate.rowid LIMIT 1))''')
            db.execute('''DELETE FROM harvest_claims WHERE rowid NOT IN (
                SELECT rowid FROM harvest_claims first
                WHERE rowid=(SELECT rowid FROM harvest_claims candidate
                    WHERE candidate.uid=first.uid ORDER BY candidate.created,candidate.rowid LIMIT 1))''')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS harvested_uid ON harvested(uid)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS harvest_claim_uid ON harvest_claims(uid)')
            # Version 1.5 stored explicit CHAT_JOIN not-found rejections as pending.
            # Only that exact saved API signature can be repaired without a new request.
            rejected = db.execute('''SELECT i.job,i.uid FROM items i
                JOIN jobs j ON j.id=i.job JOIN join_targets t ON t.job=i.job AND t.uid=i.uid
                WHERE j.kind='join' AND i.state='pending' AND i.attempts>0 AND
                (i.detail LIKE '%(код: not.found, операция: 57)' OR
                 i.detail LIKE '%(код: errors.not.found, операция: 57)')''').fetchall()
            for row in rejected:
                db.execute("UPDATE items SET state='unavailable' WHERE job=? AND uid=?", (row['job'], row['uid']))
                db.execute("DELETE FROM join_registry WHERE job=? AND uid=? AND state='pending'", (row['job'], row['uid']))
                db.execute('''UPDATE jobs SET message='Недоступная группа пропущена. Можно продолжить задание.'
                    WHERE id=? AND status IN ('needs_review','paused','interrupted')''', (row['job'],))
            db.execute("UPDATE jobs SET status='interrupted', message='Приложение закрыто во время задания; требуется ручное возобновление' WHERE status='running'")
            if not db.execute("SELECT 1 FROM settings WHERE key='join_registry_migrated'").fetchone():
                for row in db.execute('''SELECT i.job,i.uid,i.state FROM items i JOIN jobs j ON j.id=i.job
                    WHERE j.kind='join' AND i.state IN ('confirmed','pending','skipped')
                    ORDER BY CASE i.state WHEN 'confirmed' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,i.updated''').fetchall():
                    self._remember_join(db, row['job'], row['uid'], row['state'])
                db.execute("INSERT INTO settings VALUES('join_registry_migrated','1')")
            # Reservations made before a write are safe to release on process restart.
            db.execute("DELETE FROM join_registry WHERE state='reserved'")
            if not db.execute("SELECT 1 FROM settings WHERE key='join_files_migrated'").fetchone():
                for job in db.execute("SELECT id,options FROM jobs WHERE kind='join' ORDER BY created").fetchall():
                    options = json.loads(job['options'] or '{}')
                    if not options.get('file'):
                        continue
                    entries = [dict(r) for r in db.execute('SELECT link,title,source FROM join_targets WHERE job=? ORDER BY uid', (job['id'],))]
                    if entries:
                        data = json.dumps(dict(entries=entries, invalid=[], duplicates=options.get('duplicates', 0)), ensure_ascii=False)
                        digest = 'snapshot:' + hashlib.sha256((options['file'] + json.dumps([e['link'] for e in entries])).encode()).hexdigest()
                        db.execute('INSERT OR IGNORE INTO join_files VALUES(?,?,?,?,?,?,?)',
                                   (uuid.uuid4().hex, options['file'], options['file'], digest, None, data, time.time()))
                db.execute("INSERT INTO settings VALUES('join_files_migrated','1')")
            if not db.execute("SELECT 1 FROM settings WHERE key='chat_catalog_migrated'").fetchone():
                for saved in db.execute('SELECT filename,data FROM join_files').fetchall():
                    self._import_catalog(db, saved['filename'], json.loads(saved['data'])['entries'])
                for target in db.execute('''SELECT t.link,t.chat,t.title FROM join_targets t
                    WHERE t.chat IS NOT NULL ORDER BY t.rowid''').fetchall():
                    self._resolve_catalog(db, target['link'], target['chat'], target['title'])
                db.execute("INSERT INTO settings VALUES('chat_catalog_migrated','1')")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.db() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.db() as db:
            db.execute(sql, args)

    def add_account(self, name):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM accounts').fetchone()[0] >= 20:
                raise ValueError('Доступно не более 20 аккаунтов')
            identity = uuid.uuid4().hex
            db.execute("INSERT INTO accounts(id,name,connection_state) VALUES(?,?,'needs_login')", (identity, name))
        return identity

    def account_status(self, account, state, detail='', checked=False):
        from workspace_status import ACCOUNT_STATES
        if state not in ACCOUNT_STATES:
            raise ValueError('Неизвестный статус аккаунта')
        with self.db() as db:
            row = db.execute('SELECT connection_state,status_detail FROM accounts WHERE id=?', (account,)).fetchone()
            if row is None:
                return False
            changed = row['connection_state'] != state or row['status_detail'] != detail
            now = time.time()
            if changed:
                db.execute('UPDATE accounts SET connection_state=?,status_detail=?,status_changed=? WHERE id=?',
                           (state, detail, now, account))
                if row['connection_state'] != state:
                    db.execute('INSERT INTO logs(at,account,message) VALUES(?,?,?)',
                               (now, account, 'Статус: ' + ACCOUNT_STATES[state][0] + ('. ' + detail if detail else '')))
            if checked:
                db.execute('UPDATE accounts SET status_checked=? WHERE id=?', (now, account))
        return changed

    def contacts(self, account):
        return self.rows('SELECT * FROM contacts WHERE account=? ORDER BY name,uid', (account,))

    def contact(self, account, uid, name='', source='MAX', label=''):
        self.execute('''INSERT INTO contacts(account,uid,name,source,label) VALUES(?,?,?,?,?)
            ON CONFLICT(account,uid) DO UPDATE SET
            name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE contacts.name END,
            source=excluded.source,
            label=CASE WHEN excluded.label<>'' THEN excluded.label ELSE contacts.label END''',
            (account, uid, name, source, label))

    def new_job(self, account, kind, chat, amount, candidates, options=None):
        limit = 20 if kind == 'forward' else 1000
        if kind not in ('collect', 'invite', 'forward', 'join') or not 1 <= amount <= limit:
            raise ValueError('Некорректное задание')
        identity = uuid.uuid4().hex
        self.execute('''INSERT INTO jobs(id,account,kind,chat,amount,candidates,status,message,created,options)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (identity, account, kind, chat, amount, json.dumps(list(dict.fromkeys(candidates))),
             'queued', '', time.time(), json.dumps(options or {}, ensure_ascii=False)))
        return identity

    def new_join_job(self, account, entries, amount, options=None):
        from workspace_links import normalize_link
        if not entries or not 1 <= amount <= 1000:
            raise ValueError('Выберите ссылки и количество от 1 до 1000')
        unique = {}
        for entry in entries:
            link = normalize_link(entry['link'])
            if not link:
                raise ValueError('Некорректная ссылка группы')
            unique.setdefault(link, entry)
        identity = uuid.uuid4().hex
        targets = [(identity, i, link, e.get('title', ''), e.get('source', ''), None)
                   for i, (link, e) in enumerate(unique.items(), 1)]
        with self.db() as db:
            db.execute('''INSERT INTO jobs(id,account,kind,chat,amount,candidates,status,message,created,options)
                VALUES(?,?,'join',0,?,?,'queued','',?,?)''',
                (identity, account, amount, json.dumps(list(range(1, len(targets) + 1))),
                 time.time(), json.dumps(options or {}, ensure_ascii=False)))
            db.executemany('INSERT INTO join_targets VALUES(?,?,?,?,?,?)', targets)
        return identity

    def save_join_file(self, filename, original, imported):
        if not imported.entries:
            raise ValueError('В файле нет ссылок на группы MAX')
        digest = hashlib.sha256(original).hexdigest()
        data = json.dumps(dict(entries=imported.entries, invalid=imported.invalid,
                               duplicates=imported.duplicates), ensure_ascii=False)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            found = db.execute('SELECT id FROM join_files WHERE digest=?', (digest,)).fetchone()
            identity = found['id'] if found else uuid.uuid4().hex
            name = Path(filename).name
            if not found:
                db.execute('INSERT INTO join_files VALUES(?,?,?,?,?,?,?)',
                           (identity, name, name, digest, original, data, time.time()))
            added = self._import_catalog(db, name, imported.entries)
        return dict(id=identity, added=added, existing=len(imported.entries)-added,
                    invalid=len(imported.invalid), duplicates=imported.duplicates)

    @staticmethod
    def _import_catalog(db, name, entries):
        before = db.total_changes
        now = time.time()
        db.executemany('''INSERT OR IGNORE INTO chat_catalog
            (link,title,chat,state,reason,import_name,added,updated) VALUES(?,?,NULL,'active','',?,?,?)''',
            [(e['link'], e.get('title', 'Группа MAX'), name, now, now) for e in entries])
        added = db.total_changes - before
        db.executemany("INSERT OR IGNORE INTO chat_folder_links VALUES('all',?)", [(e['link'],) for e in entries])
        return added

    @staticmethod
    def _resolve_catalog(db, link, chat, title):
        db.execute('UPDATE chat_catalog SET chat=?,title=?,updated=? WHERE link=?', (chat, title, time.time(), link))
        primary = db.execute("SELECT link FROM chat_catalog WHERE chat=? AND state='active' ORDER BY added,link LIMIT 1", (chat,)).fetchone()
        if primary:
            db.execute("UPDATE chat_catalog SET state='duplicate' WHERE chat=? AND link<>? AND state='active'", (chat, primary['link']))

    def catalog_inactive(self, link):
        return bool(self.rows("SELECT 1 FROM chat_catalog WHERE link=? AND state='inactive'", (link,)))

    def invalidate_catalog_link(self, link, reason):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE chat_catalog SET state='inactive',reason=?,updated=? WHERE link=?", (reason, time.time(), link))
            row = db.execute('SELECT chat FROM chat_catalog WHERE link=?', (link,)).fetchone()
            if row and row['chat'] is not None:
                active = db.execute("SELECT 1 FROM chat_catalog WHERE chat=? AND state='active'", (row['chat'],)).fetchone()
                if not active:
                    db.execute("UPDATE chat_catalog SET state='active' WHERE link=(SELECT link FROM chat_catalog WHERE chat=? AND state='duplicate' ORDER BY added,link LIMIT 1)", (row['chat'],))

    def catalog_rows(self, folder='all', state='active', search='', limit=1000, only_free=False):
        with self.db() as db:
            db.create_function('casefold', 1, lambda value: (value or '').casefold())
            return [dict(r) for r in db.execute('''SELECT c.*,COALESCE(rc.state,rl.state,'') AS taken,
                COALESCE(a.name,rc.account_name,rl.account_name,'') AS account_name
                FROM chat_catalog c JOIN chat_folder_links f ON f.link=c.link
                LEFT JOIN join_registry rl ON rl.key='link:' || c.link
                LEFT JOIN join_registry rc ON rc.key='chat:' || c.chat
                LEFT JOIN accounts a ON a.id=COALESCE(rc.account,rl.account)
                WHERE f.folder=? AND c.state=? AND
                (instr(casefold(c.title),casefold(?))>0 OR instr(c.link,?)>0 OR CAST(c.chat AS TEXT)=?)
                AND (?=0 OR (rc.key IS NULL AND rl.key IS NULL))
                ORDER BY c.added,c.link LIMIT ?''', (folder, state, search, search, search, only_free, limit))]

    def catalog_counts(self, folder='all'):
        rows = self.rows('''SELECT c.state,COUNT(*) AS count,
            SUM(CASE WHEN rl.key IS NULL AND rc.key IS NULL THEN 1 ELSE 0 END) AS free
            FROM chat_catalog c JOIN chat_folder_links f ON f.link=c.link
            LEFT JOIN join_registry rl ON rl.key='link:' || c.link
            LEFT JOIN join_registry rc ON rc.key='chat:' || c.chat WHERE f.folder=? GROUP BY c.state''', (folder,))
        counts = {r['state']: r['count'] for r in rows}
        counts['free'] = next((r['free'] for r in rows if r['state'] == 'active'), 0)
        return counts

    def join_files(self):
        return self.rows('''SELECT id,name,filename,created,json_array_length(data,'$.entries') AS count
            FROM join_files ORDER BY created DESC,id''')

    def join_file(self, identity):
        from workspace_links import LinkImport
        rows = self.rows('SELECT name,data FROM join_files WHERE id=?', (identity,))
        if not rows:
            raise ValueError('Сохранённый файл не найден')
        return rows[0]['name'], LinkImport(**json.loads(rows[0]['data']))

    @staticmethod
    def _remember_join(db, job, uid, state):
        row = db.execute('''SELECT t.*,j.account,COALESCE(a.name,j.account) AS account_name
            FROM join_targets t JOIN jobs j ON j.id=t.job LEFT JOIN accounts a ON a.id=j.account
            WHERE t.job=? AND t.uid=?''', (job, uid)).fetchone()
        if not row:
            return
        keys = ['link:' + row['link']]
        if row['chat'] is not None:
            keys.append('chat:' + str(row['chat']))
        for key in keys:
            db.execute('''INSERT INTO join_registry VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                state=excluded.state,title=excluded.title,updated=excluded.updated
                WHERE join_registry.job=excluded.job AND join_registry.uid=excluded.uid''',
                (key, row['account'], row['account_name'], job, uid, state, row['title'], time.time()))

    def claim_join(self, job, uid, link, chat=None):
        keys = ['link:' + link] + (['chat:' + str(chat)] if chat is not None else [])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for key in keys:
                previous = db.execute('SELECT * FROM join_registry WHERE key=?', (key,)).fetchone()
                if previous and (previous['job'], previous['uid']) != (job, uid):
                    return dict(previous)
            account = db.execute('''SELECT j.account,COALESCE(a.name,j.account) AS name FROM jobs j
                LEFT JOIN accounts a ON a.id=j.account WHERE j.id=?''', (job,)).fetchone()
            for key in keys:
                db.execute('INSERT OR IGNORE INTO join_registry VALUES(?,?,?,?,?,?,?,?)',
                           (key, account['account'], account['name'], job, uid, 'reserved', '', time.time()))
        return None

    def release_join_reservations(self, job, uid=None):
        self.execute("DELETE FROM join_registry WHERE job=? AND state='reserved' AND (? IS NULL OR uid=?)",
                     (job, uid, uid))

    def global_join_rows(self):
        return self.rows('''SELECT r.account_name,r.account,r.job,r.uid,r.title,r.state,r.updated,
            MAX(CASE WHEN r.key LIKE 'chat:%' THEN substr(r.key,6) ELSE '' END) AS chat,
            MAX(CASE WHEN r.key LIKE 'link:%' THEN substr(r.key,6) ELSE '' END) AS link
            FROM join_registry r GROUP BY r.job,r.uid ORDER BY r.updated DESC''')

    def join_rows(self, identity):
        return self.rows('''SELECT t.*,COALESCE(i.state,'queued') AS state,
            COALESCE(i.detail,'') AS detail,COALESCE(i.attempts,0) AS attempts,
            COALESCE(i.updated,0) AS updated FROM join_targets t
            LEFT JOIN items i ON i.job=t.job AND i.uid=t.uid WHERE t.job=? ORDER BY t.uid''', (identity,))

    def join_history(self, account, search='', limit=1000, state=''):
        with self.db() as db:
            db.create_function('casefold', 1, lambda value: (value or '').casefold())
            return [dict(row) for row in db.execute('''SELECT t.*,i.state,i.detail,i.updated,i.attempts,j.status
            FROM join_targets t JOIN items i ON i.job=t.job AND i.uid=t.uid
            JOIN jobs j ON j.id=t.job WHERE j.account=? AND
            (instr(casefold(t.title),casefold(?))>0 OR instr(casefold(t.link),casefold(?))>0)
            AND (?='' OR i.state=?)
            ORDER BY i.updated DESC LIMIT ?''', (account, search, search, state, state, limit))]

    def previous_join(self, account, link, chat=None, exclude=None):
        rows = self.rows('''SELECT t.*,i.state FROM join_targets t
            JOIN items i ON i.job=t.job AND i.uid=t.uid JOIN jobs j ON j.id=t.job
            WHERE j.account=? AND (t.link=? OR (? IS NOT NULL AND t.chat=?))
            AND NOT (t.job=? AND t.uid=?) AND i.state IN ('confirmed','pending','skipped')
            ORDER BY CASE i.state WHEN 'confirmed' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END LIMIT 1''',
            (account, link, chat, chat, *(exclude or ('', 0))))
        return rows[0] if rows else None

    def join_target_info(self, job, uid, chat, title):
        with self.db() as db:
            db.execute('UPDATE join_targets SET chat=?,title=? WHERE job=? AND uid=?', (chat, title, job, uid))
            row = db.execute('SELECT link FROM join_targets WHERE job=? AND uid=?', (job, uid)).fetchone()
            if row:
                self._resolve_catalog(db, row['link'], chat, title)

    def job(self, identity):
        return self.rows('SELECT * FROM jobs WHERE id=?', (identity,))[0]

    def status(self, identity, state, message=''):
        self.execute('UPDATE jobs SET status=?,message=? WHERE id=?', (state, message, identity))

    def set_job_options(self, identity, options):
        self.execute('UPDATE jobs SET options=? WHERE id=?',
                     (json.dumps(options, ensure_ascii=False), identity))

    def item(self, job, uid, state, detail='', attempted=False):
        with self.db() as db:
            db.execute('''INSERT INTO items(job,uid,state,detail,attempts,updated)
            VALUES(?,?,?,?,?,?) ON CONFLICT(job,uid) DO UPDATE SET
            state=excluded.state,
            detail=CASE WHEN excluded.detail<>'' THEN excluded.detail ELSE items.detail END,
            attempts=items.attempts+excluded.attempts,
            updated=excluded.updated''',
                (job, uid, state, detail, 1 if attempted else 0, time.time()))
            if state in ('confirmed', 'pending') or (state == 'skipped' and detail == 'Аккаунт уже состоит в группе'):
                self._remember_join(db, job, uid, state)
            elif state in ('unavailable', 'retryable'):
                db.execute("DELETE FROM join_registry WHERE job=? AND uid=? AND state IN ('reserved','pending')", (job, uid))

    def items(self, job):
        return {row['uid']: row['state'] for row in self.rows('SELECT * FROM items WHERE job=?', (job,))}

    def item_rows(self, job):
        return self.rows('SELECT uid,state,detail,attempts,updated FROM items WHERE job=? ORDER BY updated,uid',
                         (job,))

    def update_source(self, chat, title='', total=0):
        self.execute('''INSERT INTO sources(chat,title,total,updated) VALUES(?,?,?,?)
            ON CONFLICT(chat) DO UPDATE SET
            title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE sources.title END,
            total=CASE WHEN excluded.total>0 THEN excluded.total ELSE sources.total END,
            updated=excluded.updated''', (chat, title, total, time.time()))

    def harvested_ids(self, source=None):
        query = 'SELECT uid FROM harvested' if source is None else 'SELECT uid FROM harvested WHERE source=?'
        args = () if source is None else (source,)
        return {row['uid'] for row in self.rows(query, args)}

    def ignored_ids(self):
        return {r['uid'] for r in self.rows('SELECT uid FROM ignored')}

    def save_template(self, name, amount, sources):
        if not name.strip() or not 1 <= amount <= 1000 or not sources:
            raise ValueError('Укажите название, аккаунты и количество контактов')
        self.execute('INSERT INTO collection_templates VALUES(?,?) ON CONFLICT(name) DO UPDATE SET data=excluded.data',
                     (name.strip(), json.dumps({'amount': amount, 'sources': dict(sources)})))

    def templates(self):
        return {r['name']: json.loads(r['data']) for r in self.rows('SELECT * FROM collection_templates ORDER BY name')}

    def favorites(self, account):
        return {r['chat'] for r in self.rows('SELECT chat FROM favorite_chats WHERE account=?', (account,))}

    def set_favorite(self, account, chat, enabled):
        if enabled:
            self.execute('INSERT OR IGNORE INTO favorite_chats VALUES(?,?)', (account, chat))
        else:
            self.execute('DELETE FROM favorite_chats WHERE account=? AND chat=?', (account, chat))

    def batch_report(self, identity):
        job = self.job(identity)
        group = json.loads(job.get('options') or '{}').get('group')
        return self.rows('''SELECT j.id,j.account,j.chat,j.amount,j.status,j.deleted,
            COALESCE(a.name,j.account) AS account_name,COALESCE(s.title,CAST(j.chat AS TEXT)) AS source,
            SUM(CASE WHEN i.state='confirmed' THEN 1 ELSE 0 END) AS done,
            SUM(CASE WHEN i.state='pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN i.state='unavailable' THEN 1 ELSE 0 END) AS unavailable,
            SUM(CASE WHEN i.state='unavailable' AND g.uid IS NOT NULL THEN 1 ELSE 0 END) AS ignored
            FROM jobs j LEFT JOIN accounts a ON a.id=j.account
            LEFT JOIN sources s ON s.chat=j.chat
            LEFT JOIN items i ON i.job=j.id LEFT JOIN ignored g ON g.uid=i.uid AND j.kind='collect'
            WHERE (j.id=? AND ? IS NULL) OR (? IS NOT NULL AND json_extract(j.options,'$.group')=?)
            GROUP BY j.id ORDER BY j.created,j.id''', (identity, group, group, group))

    def is_ignored(self, uid):
        return bool(self.rows('SELECT 1 FROM ignored WHERE uid=?', (uid,)))

    def source_page(self, account, chat, marker):
        rows = self.rows('SELECT * FROM source_pages WHERE account=? AND chat=? AND marker=? AND created>?',
                         (account, chat, marker or 0, time.time() - 300))
        return rows[0] if rows else None

    def clear_source_pages(self, account, chat):
        self.execute('DELETE FROM source_pages WHERE account=? AND chat=?', (account, chat))

    def save_source_page(self, account, chat, marker, members, next_marker):
        self.execute('INSERT OR REPLACE INTO source_pages VALUES(?,?,?,?,?,?)',
                     (account, chat, marker or 0, json.dumps(members, ensure_ascii=False), next_marker or 0, time.time()))

    def confirm_collection(self, job, uid, name, label, done):
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''INSERT INTO contacts(account,uid,name,source,label) VALUES(?,?,?,?,?)
                ON CONFLICT(account,uid) DO UPDATE SET name=excluded.name,source=excluded.source,
                label=CASE WHEN excluded.label<>'' THEN excluded.label ELSE contacts.label END''',
                (job['account'], uid, name, str(job['chat'] or 'Вручную'), label))
            db.execute("UPDATE items SET state='confirmed',detail='MAX подтвердил добавление контакта',updated=? WHERE job=? AND uid=?", (now, job['id'], uid))
            db.execute('INSERT OR IGNORE INTO harvested VALUES(?,?,?,?,?)', (job['chat'], uid, job['account'], job['id'], now))
            db.execute('DELETE FROM harvest_claims WHERE uid=? AND job=?', (uid, job['id']))
            db.execute("UPDATE jobs SET message=? WHERE id=?", (f'Добавлено {done} / {job["amount"]}', job['id']))

    def save_metrics(self, job, metrics):
        self.execute('''INSERT INTO job_metrics VALUES(?,?,?,?,?,?) ON CONFLICT(job) DO UPDATE SET
            elapsed=elapsed+excluded.elapsed, waiting=waiting+excluded.waiting,
            network=network+excluded.network, requests=requests+excluded.requests,
            cached_pages=cached_pages+excluded.cached_pages''',
            (job, metrics['elapsed'], metrics['waiting'], metrics['network'], metrics['requests'], metrics['cached_pages']))

    def append_candidate(self, identity, uid):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT candidates FROM jobs WHERE id=?', (identity,)).fetchone()
            candidates = json.loads(row['candidates'])
            if uid not in candidates:
                candidates.append(uid)
                db.execute('UPDATE jobs SET candidates=? WHERE id=?', (json.dumps(candidates), identity))

    def ignore(self, uid, reason):
        self.execute('INSERT OR REPLACE INTO ignored VALUES(?,?,?)', (uid, reason, time.time()))

    def delete_job(self, identity):
        with self.db() as db:
            job = db.execute('SELECT status FROM jobs WHERE id=?', (identity,)).fetchone()
            if not job or job['status'] == 'running':
                raise ValueError('Сначала остановите задание')
            db.execute('UPDATE jobs SET deleted=1 WHERE id=?', (identity,))

    def claim_harvest(self, source, uid, account, job):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM harvested WHERE uid=?', (uid,)).fetchone():
                return 'confirmed'
            claim = db.execute('SELECT job FROM harvest_claims WHERE uid=?', (uid,)).fetchone()
            if claim:
                return 'owned' if claim['job'] == job else 'pending'
            db.execute('INSERT INTO harvest_claims VALUES(?,?,?,?,?)',
                       (source, uid, account, job, time.time()))
            return 'new'

    def confirm_harvest(self, source, uid, account, job):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM harvested WHERE uid=?', (uid,)).fetchone():
                db.execute('INSERT INTO harvested VALUES(?,?,?,?,?)',
                           (source, uid, account, job, time.time()))
            db.execute('DELETE FROM harvest_claims WHERE source=? AND uid=? AND job=?',
                       (source, uid, job))

    def release_harvest(self, source, uid, job):
        self.execute('DELETE FROM harvest_claims WHERE source=? AND uid=? AND job=?',
                     (source, uid, job))

    def source_rows(self):
        return self.rows('''SELECT s.chat,s.title,s.total,s.updated,
            COUNT(h.uid) AS taken,COUNT(DISTINCT h.account) AS accounts,MAX(h.added) AS last_added
            FROM sources s LEFT JOIN harvested h ON h.source=s.chat
            GROUP BY s.chat ORDER BY COALESCE(MAX(h.added),s.updated) DESC''')

    def source_members(self, source):
        return self.rows('''SELECT h.uid,h.added,h.job,COALESCE(a.name,h.account) AS account
            FROM harvested h LEFT JOIN accounts a ON a.id=h.account
            WHERE h.source=? ORDER BY h.added DESC,h.uid''', (source,))

    def confirmed_group_members(self, chat):
        return {row['uid'] for row in self.rows('''SELECT DISTINCT i.uid
            FROM items i JOIN jobs j ON j.id=i.job
            WHERE j.kind='invite' AND j.chat=? AND i.state='confirmed' ''', (chat,))}

    def log(self, account, message):
        self.execute('INSERT INTO logs(at,account,message) VALUES(?,?,?)', (time.time(), account, message))

    def setting(self, key, default):
        rows = self.rows('SELECT value FROM settings WHERE key=?', (key,))
        return rows[0]['value'] if rows else default

    def set_setting(self, key, value):
        self.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
