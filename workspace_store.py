import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'workspace.sqlite'
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
            ):
                if name not in account_columns:
                    db.execute(f'ALTER TABLE accounts ADD COLUMN {name} {definition}')
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
            db.execute("UPDATE jobs SET status='interrupted', message='Приложение закрыто во время задания; требуется ручное возобновление' WHERE status='running'")

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
            db.execute('INSERT INTO accounts(id,name) VALUES(?,?)', (identity, name))
        return identity

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
        if kind not in ('collect', 'invite', 'forward') or not 1 <= amount <= limit:
            raise ValueError('Некорректное задание')
        identity = uuid.uuid4().hex
        self.execute('''INSERT INTO jobs(id,account,kind,chat,amount,candidates,status,message,created,options)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (identity, account, kind, chat, amount, json.dumps(list(dict.fromkeys(candidates))),
             'queued', '', time.time(), json.dumps(options or {}, ensure_ascii=False)))
        return identity

    def job(self, identity):
        return self.rows('SELECT * FROM jobs WHERE id=?', (identity,))[0]

    def status(self, identity, state, message=''):
        self.execute('UPDATE jobs SET status=?,message=? WHERE id=?', (state, message, identity))

    def set_job_options(self, identity, options):
        self.execute('UPDATE jobs SET options=? WHERE id=?',
                     (json.dumps(options, ensure_ascii=False), identity))

    def item(self, job, uid, state, detail='', attempted=False):
        self.execute('''INSERT INTO items(job,uid,state,detail,attempts,updated)
            VALUES(?,?,?,?,?,?) ON CONFLICT(job,uid) DO UPDATE SET
            state=excluded.state,
            detail=CASE WHEN excluded.detail<>'' THEN excluded.detail ELSE items.detail END,
            attempts=items.attempts+excluded.attempts,
            updated=excluded.updated''',
            (job, uid, state, detail, 1 if attempted else 0, time.time()))

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
            LEFT JOIN items i ON i.job=j.id LEFT JOIN ignored g ON g.uid=i.uid
            WHERE (j.id=? AND ? IS NULL) OR (? IS NOT NULL AND json_extract(j.options,'$.group')=?)
            GROUP BY j.id ORDER BY j.created,j.id''', (identity, group, group, group))

    def is_ignored(self, uid):
        return bool(self.rows('SELECT 1 FROM ignored WHERE uid=?', (uid,)))

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
