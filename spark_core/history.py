"""Opt-in, owner-scoped summaries. Raw profiles are never persisted here."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager


class History:
    def __init__(self, path, enabled=False, retention_days=30):
        self.path, self.enabled, self.days = path, enabled, retention_days

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, owner TEXT, server TEXT, problem TEXT, created REAL, overview TEXT, result TEXT)')
                db.execute('DELETE FROM reviews WHERE created < ?', (time.time()-self.days*86400,))
                yield db
        finally:
            db.close()

    def save(self, owner, server, problem, overview, result):
        if not self.enabled:
            return None
        key = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute('INSERT INTO reviews VALUES (?,?,?,?,?,?,?)',
                       (key, owner, server[:80], problem[:80], time.time(),
                        json.dumps(overview, ensure_ascii=False), result[:24000]))
        return key

    def list(self, owner, server, problem=''):
        if not self.enabled or not self.path.exists():
            return []
        with self.connect() as db:
            rows = db.execute("SELECT id, created, overview, result FROM reviews WHERE owner=? AND server=? AND (?='' OR problem=?) ORDER BY created DESC LIMIT 10",
                              (owner, server, problem, problem)).fetchall()
        return [{'id': r[0], 'created': r[1], 'overview': json.loads(r[2]), 'result': r[3]} for r in rows]

    def purge(self):
        # Retention applies even while saving is disabled; never creates the database.
        if not self.path.exists():
            return 0
        with self.connect() as db:
            return db.total_changes

    def delete(self, owner):
        # Works while disabled so users can still erase rows written earlier.
        if not self.path.exists():
            return 0
        with self.connect() as db:
            cursor = db.execute('DELETE FROM reviews WHERE owner=?', (owner,))
            return cursor.rowcount


def compare(old, new):
    # Overviews saved by older versions may lack a section; missing fields count as different conditions.
    def section(overview, name):
        return overview.get(name) or {}
    reasons = []
    for key in ('name', 'version', 'minecraft', 'type'):
        if section(old, 'platform').get(key) != section(new, 'platform').get(key):
            reasons.append('platform.'+key)
    for key in ('mode', 'engine', 'interval_us', 'aggregator', 'tick_threshold'):
        if section(old, 'sampling').get(key) != section(new, 'sampling').get(key):
            reasons.append('sampling.'+key)
    delta = {}
    for key, value in section(new, 'health').items():
        before = section(old, 'health').get(key)
        delta[key] = value-before if value is not None and before is not None else None
    return {'comparable_sampling': not reasons, 'different_conditions': reasons,
            'health_delta': delta, 'warning': '滚动健康指标不是完整采样期均值；玩家/区块负载及现象覆盖仍需人工确认，不证明优化因果'}
