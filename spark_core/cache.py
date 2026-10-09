import hashlib
import sqlite3
import time
from contextlib import contextmanager

class ProfileCache:
    """Raw BLOB database, independent of review history; sliding access expiry."""
    def __init__(self, path, hours=72):
        self.path = path
        self.hours = max(1, min(8760, float(hours)))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS profiles (report_id TEXT PRIMARY KEY, data BLOB NOT NULL, sha256 TEXT NOT NULL, downloaded_at REAL NOT NULL, last_access REAL NOT NULL)')
                db.execute('DELETE FROM profiles WHERE last_access <= ?', (time.time()-self.hours*3600,))
                yield db
        finally:
            db.close()

    def get(self, key, target):
        with self.connect() as db:
            row = db.execute('SELECT data, sha256 FROM profiles WHERE report_id=?', (key,)).fetchone()
            if not row:
                return False
            raw, checksum = row
            if not raw or len(raw)>128*1024*1024 or hashlib.sha256(raw).hexdigest()!=checksum:
                db.execute('DELETE FROM profiles WHERE report_id=?', (key,))
                return False
            target.write_bytes(raw)
            db.execute('UPDATE profiles SET last_access=? WHERE report_id=?', (time.time(), key))
        return True

    def discard(self, key):
        with self.connect() as db:
            db.execute('DELETE FROM profiles WHERE report_id=?', (key,))

    def put(self, key, source):
        if not 0 < source.stat().st_size <= 128*1024*1024:
            raise ValueError('缓存报告为空或超限')
        raw = source.read_bytes()
        now = time.time()
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?)', (key, raw, hashlib.sha256(raw).hexdigest(), now, now))
