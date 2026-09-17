import base64
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet
from PySide6.QtCore import QLockFile
from workspace_security import Vault, crypt
from workspace_store import Store
from transfer_workspace import export_workspace, restore_workspace, read_archive, snapshot


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.store = Store(self.source)
        self.account = self.store.add_account('Test')
        self.store.contact(self.account, 100, 'Contact', 'Group')
        self.vault = Vault(self.source / 'sessions')
        folder = self.vault.folder(self.account)
        with closing(sqlite3.connect(folder / 'session.db')) as db:
            db.execute('CREATE TABLE session(token TEXT)')
            db.execute('INSERT INTO session VALUES(?)', ('test-session-secret',))
            db.commit()
        self.vault.seal(self.account)
        self.output = self.root / 'output'
        self.target = self.root / 'target'

    def tearDown(self):
        self.temp.cleanup()

    def export(self):
        return export_workspace(self.source, self.output)

    def paths(self):
        return self.output / 'workspace.maxbackup', self.output / 'workspace.recovery-key'

    def test_roundtrip_preserves_history_and_reprotects_sessions(self):
        self.assertEqual(self.export(), dict(accounts=1, sessions=1, orphan_sessions=0))
        archive, key = self.paths()
        self.assertNotIn(b'test-session-secret', archive.read_bytes())
        self.assertEqual(restore_workspace(archive, key, self.target), dict(accounts=1, sessions=1, orphan_sessions=0))
        restored = Store(self.target)
        self.assertEqual(restored.contacts(self.account)[0]['uid'], 100)
        protected = self.target / 'sessions' / self.account / 'session.protected'
        data = crypt(protected.read_bytes(), decrypt=True)
        self.assertIn(b'test-session-secret', data)
        self.assertFalse((protected.parent / 'session.db').exists())
        self.assertFalse((self.vault.folder(self.account) / 'session.db').exists())

    def test_wrong_key_and_tamper_do_not_touch_target(self):
        self.export()
        archive, key = self.paths()
        wrong = self.root / 'wrong.recovery-key'
        wrong.write_bytes(Fernet.generate_key())
        with self.assertRaises(ValueError):
            restore_workspace(archive, wrong, self.target)
        self.assertFalse(self.target.exists())
        data = bytearray(archive.read_bytes())
        data[len(data) // 2] ^= 1
        archive.write_bytes(data)
        with self.assertRaises(ValueError):
            restore_workspace(archive, key, self.target)
        self.assertFalse(self.target.exists())

    def test_never_overwrites_existing_workspace_or_export(self):
        self.export()
        before = snapshot(self.source / 'workspace.sqlite')
        with self.assertRaises(ValueError):
            restore_workspace(*self.paths(), self.source)
        with self.assertRaises(ValueError):
            self.export()
        self.assertEqual(snapshot(self.source / 'workspace.sqlite'), before)

    def test_running_app_lock_blocks_export_and_import(self):
        self.export()
        lock = QLockFile(str(self.source / 'workspace.lock'))
        self.assertTrue(lock.tryLock(100))
        try:
            with self.assertRaises(ValueError):
                export_workspace(self.source, self.root / 'another')
            with self.assertRaises(ValueError):
                restore_workspace(*self.paths(), self.source)
        finally:
            lock.unlock()

    def test_plaintext_crash_recovery_session_can_be_exported(self):
        self.vault.restore(self.account)
        self.export()
        _, sessions, _ = read_archive(*self.paths())
        self.assertIn(b'test-session-secret', sessions[self.account])

    def test_wal_session_roundtrip(self):
        folder = self.vault.restore(self.account)
        with closing(sqlite3.connect(folder / 'session.db')) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('INSERT INTO session VALUES(?)', ('wal-secret',))
            db.commit()
        self.vault.seal(self.account)
        self.export()
        restore_workspace(*self.paths(), self.target)
        restored = Vault(self.target / 'sessions').restore(self.account)
        with closing(sqlite3.connect(restored / 'session.db')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM session').fetchone()[0], 2)

    def test_orphan_sessions_preserved_and_recovered_only_when_requested(self):
        self.store.execute('DELETE FROM accounts')
        self.assertEqual(self.export(), dict(accounts=0, sessions=1, orphan_sessions=1))
        restored = restore_workspace(*self.paths(), self.target)
        self.assertEqual(restored['accounts'], 0)
        second = self.root / 'recovered'
        restored = restore_workspace(*self.paths(), second, recover_orphans=True)
        self.assertEqual(restored['accounts'], 1)
        self.assertEqual(Store(second).rows('SELECT id FROM accounts')[0]['id'], self.account)
        self.assertEqual(self.store.rows('SELECT id FROM accounts'), [])

    def test_legacy_session_can_be_recovered(self):
        legacy = self.vault.restore(self.account) / 'session.db'
        result = export_workspace(self.source, self.output, legacy_session=legacy)
        self.assertEqual(result['sessions'], 2)
        self.assertEqual(result['orphan_sessions'], 1)
        restored = restore_workspace(*self.paths(), self.target, recover_orphans=True)
        self.assertEqual(restored['accounts'], 2)

    def test_session_path_traversal_is_rejected_before_writing(self):
        self.export()
        archive, key = self.paths()
        cipher = Fernet(key.read_bytes().strip())
        payload = json.loads(cipher.decrypt(archive.read_bytes()))
        payload['sessions']['../outside'] = payload['sessions'][self.account]
        archive.write_bytes(cipher.encrypt(json.dumps(payload).encode()))
        with self.assertRaises(ValueError):
            restore_workspace(archive, key, self.target)
        self.assertFalse(self.target.exists())

    def test_unknown_format_and_corrupt_database_rejected(self):
        self.export()
        archive, key = self.paths()
        cipher = Fernet(key.read_bytes().strip())
        payload = json.loads(cipher.decrypt(archive.read_bytes()))
        payload['database'] = base64.b64encode(b'bad database').decode()
        archive.write_bytes(cipher.encrypt(json.dumps(payload).encode()))
        with self.assertRaises(ValueError):
            read_archive(archive, key)
