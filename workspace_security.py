import ctypes
import os
import sqlite3
from pathlib import Path
from ctypes import wintypes
from contextlib import closing


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def crypt(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('Защита сессий поддерживается только в Windows')
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(result.data)


class Vault:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def folder(self, account):
        if len(account) != 32 or any(c not in '0123456789abcdef' for c in account):
            raise ValueError('Некорректный идентификатор аккаунта')
        folder = self.directory / account
        folder.mkdir(exist_ok=True)
        return folder

    def restore(self, account):
        folder = self.folder(account)
        path = folder / 'session.db'
        protected = folder / 'session.protected'
        # Recover a session left open by an abnormal shutdown rather than discard it.
        if protected.exists() and not path.exists():
            path.write_bytes(crypt(protected.read_bytes(), decrypt=True))
        return folder

    def has_session(self, account):
        folder = self.folder(account)
        return (folder / 'session.protected').exists() or (folder / 'session.db').exists()

    def seal(self, account):
        folder = self.folder(account)
        path = folder / 'session.db'
        if not path.exists():
            return
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        temporary = folder / 'session.protected.new'
        temporary.write_bytes(crypt(path.read_bytes()))
        temporary.replace(folder / 'session.protected')
        for name in ('session.db', 'session.db-wal', 'session.db-shm'):
            (folder / name).unlink(missing_ok=True)

    def import_legacy(self, account, path):
        folder = self.folder(account)
        source_uri = Path(path).resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            with closing(sqlite3.connect(folder / 'session.db')) as target:
                source.backup(target)
        self.seal(account)
