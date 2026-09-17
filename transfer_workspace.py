"""Offline encrypted transfer of workspace data and Windows-protected sessions."""
import argparse
import base64
import json
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from PySide6.QtCore import QLockFile

from workspace_security import crypt


MAX_BYTES = 512 * 1024 * 1024
ACCOUNT_ID = re.compile(r'[0-9a-f]{32}')


@contextmanager
def workspace_lock(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(directory / 'workspace.lock'))
    if not lock.tryLock(100):
        raise ValueError('Close MAX Workspace before transferring data.')
    try:
        yield directory
    finally:
        lock.unlock()


def snapshot(path):
    if not path.is_file():
        raise ValueError('Workspace database is missing.')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(':memory:')) as target:
            source.backup(target)
            return portable_database(target.serialize())


def portable_database(data):
    if not isinstance(data, bytes) or len(data) < 100 or not data.startswith(b'SQLite format 3\0'):
        raise ValueError('Invalid database in transfer archive.')
    # SQLite deserialize cannot open WAL images. Backups and sealed (checkpointed)
    # sessions are standalone: mark header read/write versions as rollback-journal.
    if data[18:20] == b'\x02\x02':
        return data[:18] + b'\x01\x01' + data[20:]
    return data


def validate_database(data, workspace=False):
    data = portable_database(data)
    with closing(sqlite3.connect(':memory:')) as db:
        db.deserialize(data)
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed.')
        if workspace:
            accounts = [row[0] for row in db.execute('SELECT id FROM accounts')]
            if len(accounts) > 20 or any(not isinstance(a, str) or not ACCOUNT_ID.fullmatch(a) for a in accounts):
                raise ValueError('Invalid account list in transfer archive.')
            return accounts
    return []


def export_workspace(directory, output, legacy_session=None):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Choose a new output folder; existing files will not be overwritten.')
    with workspace_lock(directory) as source:
        database = snapshot(source / 'workspace.sqlite')
        accounts = validate_database(database, workspace=True)
        sessions = {}
        orphan_accounts = []
        folders = source / 'sessions'
        found = {p.name for p in folders.iterdir() if p.is_dir() and ACCOUNT_ID.fullmatch(p.name)} if folders.exists() else set()
        for account in sorted(set(accounts) | found):
            folder = source / 'sessions' / account
            if (folder / 'session.db').is_file():
                data = snapshot(folder / 'session.db')
            elif (folder / 'session.protected').is_file():
                data = portable_database(crypt((folder / 'session.protected').read_bytes(), decrypt=True))
            else:
                continue
            validate_database(data)
            sessions[account] = base64.b64encode(data).decode('ascii')
            if account not in accounts:
                orphan_accounts.append(account)
        if legacy_session is not None:
            data = snapshot(Path(legacy_session))
            validate_database(data)
            identity = uuid.uuid4().hex
            orphan_accounts.append(identity)
            sessions[identity] = base64.b64encode(data).decode('ascii')
        payload = json.dumps(dict(format='max-workspace-transfer', version=1,
                                  created=datetime.now(timezone.utc).isoformat(),
                                  database=base64.b64encode(database).decode('ascii'),
                                  orphan_accounts=orphan_accounts,
                                  sessions=sessions), separators=(',', ':')).encode('utf-8')
        if len(payload) > MAX_BYTES // 2:
            raise ValueError('Workspace is too large for this transfer format.')
        key = Fernet.generate_key()
        encrypted = Fernet(key).encrypt(payload)
        output.mkdir(parents=True, exist_ok=False)
        # No plaintext session file is created. The key is separate from the encrypted archive.
        with (output / 'workspace.recovery-key').open('xb') as file:
            file.write(key + b'\n')
        with (output / 'workspace.maxbackup').open('xb') as file:
            file.write(encrypted)
    return dict(accounts=len(accounts), sessions=len(sessions), orphan_sessions=len(orphan_accounts))


def read_archive(archive, key_file):
    archive, key_file = Path(archive), Path(key_file)
    if archive.stat().st_size > MAX_BYTES or key_file.stat().st_size > 1024:
        raise ValueError('Transfer file is too large.')
    try:
        raw = Fernet(key_file.read_bytes().strip()).decrypt(archive.read_bytes())
    except (InvalidToken, ValueError):
        raise ValueError('Wrong recovery key or damaged transfer archive.') from None
    try:
        payload = json.loads(raw)
        if payload['format'] != 'max-workspace-transfer' or payload['version'] != 1:
            raise ValueError('Unsupported transfer format.')
        database = portable_database(base64.b64decode(payload['database'], validate=True))
        accounts = validate_database(database, workspace=True)
        orphans = payload.get('orphan_accounts', [])
        if (not isinstance(orphans, list) or len(orphans) > 100 or
                any(not isinstance(a, str) or not ACCOUNT_ID.fullmatch(a) or a in accounts for a in orphans) or
                len(set(orphans)) != len(orphans)):
            raise ValueError('Invalid orphan session list.')
        if not isinstance(payload['sessions'], dict):
            raise ValueError('Invalid session list.')
        sessions = {}
        for account, encoded in payload['sessions'].items():
            if account not in accounts + orphans or not ACCOUNT_ID.fullmatch(account):
                raise ValueError('Session does not belong to this workspace.')
            sessions[account] = portable_database(base64.b64decode(encoded, validate=True))
            validate_database(sessions[account])
        if not set(orphans) <= sessions.keys():
            raise ValueError('Missing orphan session data.')
        return database, sessions, accounts
    except (KeyError, TypeError, sqlite3.Error, UnicodeError) as error:
        raise ValueError('Invalid transfer archive structure.') from error


def restore_workspace(archive, key_file, directory, recover_orphans=False):
    database, sessions, accounts = read_archive(archive, key_file)
    orphans = sorted(set(sessions) - set(accounts))
    if recover_orphans and len(accounts) + len(orphans) > 20:
        raise ValueError('Recovering orphan sessions would exceed the 20-account limit.')
    with workspace_lock(directory) as target:
        # Restore only into a new workspace. Never merge account IDs or replace laptop data.
        if any(p.name != 'workspace.lock' for p in target.iterdir()):
            raise ValueError('Target workspace is not empty. Back it up or rename it before restoring.')
        with tempfile.TemporaryDirectory(prefix='max-restore-', dir=target.parent) as temporary:
            stage = Path(temporary)
            (stage / 'sessions').mkdir()
            for account, data in sessions.items():
                folder = stage / 'sessions' / account
                folder.mkdir()
                (folder / 'session.protected').write_bytes(crypt(data))
            # SQLite backup materializes a standalone database even if the source used WAL.
            with closing(sqlite3.connect(':memory:')) as memory:
                memory.deserialize(database)
                if recover_orphans:
                    with memory:
                        for number, account in enumerate(orphans, 1):
                            memory.execute('INSERT INTO accounts(id,name) VALUES(?,?)',
                                           (account, f'Восстановленный {number}'))
                with closing(sqlite3.connect(stage / 'workspace.sqlite')) as db:
                    memory.backup(db)
                    db.execute('PRAGMA journal_mode=DELETE')
            (stage / 'sessions').rename(target / 'sessions')
            try:
                (stage / 'workspace.sqlite').rename(target / 'workspace.sqlite')
            except BaseException:
                (target / 'sessions').rename(stage / 'sessions')
                raise
    return dict(accounts=len(accounts) + (len(orphans) if recover_orphans else 0),
                sessions=len(sessions), orphan_sessions=0 if recover_orphans else len(orphans))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    default = Path(os.environ.get('LOCALAPPDATA', '.')) / 'MAX-Workspace'
    export = sub.add_parser('export')
    export.add_argument('--data-dir', type=Path, default=default)
    export.add_argument('--output', required=True, type=Path)
    export.add_argument('--legacy-session', type=Path)
    restore = sub.add_parser('restore')
    restore.add_argument('--archive', required=True, type=Path)
    restore.add_argument('--key-file', required=True, type=Path)
    restore.add_argument('--data-dir', type=Path, default=default)
    restore.add_argument('--recover-orphans', action='store_true',
                         help='Create locally named accounts for sessions missing from the database.')
    verify = sub.add_parser('verify')
    verify.add_argument('--archive', required=True, type=Path)
    verify.add_argument('--key-file', required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'export':
            result = export_workspace(args.data_dir, args.output, args.legacy_session)
        elif args.command == 'restore':
            result = restore_workspace(args.archive, args.key_file, args.data_dir, args.recover_orphans)
        else:
            _, sessions, accounts = read_archive(args.archive, args.key_file)
            result = dict(accounts=len(accounts), sessions=len(sessions),
                          orphan_sessions=len(set(sessions) - set(accounts)))
    except (ValueError, OSError, sqlite3.Error):
        # Exceptions may contain paths or database values; never echo credential-bearing data.
        parser.exit(1, 'Transfer failed. Close the app, check the archive/key and use a new target folder. Existing workspace data was not replaced.\n')
    print(f"{args.command}: OK; accounts: {result['accounts']}; session databases: {result['sessions']}")
    if result['orphan_sessions']:
        print(f"Sessions without account records: {result['orphan_sessions']}. Use restore --recover-orphans to recover them with temporary names.")
    if args.command == 'export':
        print('Keep the archive and recovery key private. Transfer the key separately. Do not upload either file to GitHub.')


if __name__ == '__main__':
    main()
