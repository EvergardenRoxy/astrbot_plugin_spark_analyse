"""Once-per-chat delivery ledger and supervised chart lifecycle."""
import asyncio
import hashlib
import json
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from astrbot.api import logger


class DeliveryLedger:
    def __init__(self, path, retention_days=30):
        self.path = Path(path)
        self.retention = retention_days*86400
        self.sent = {}  # (chat, fingerprint) -> time. Fail-safe after a send whose DB acknowledgement fails.

    def connect(self):
        db = sqlite3.connect(self.path, timeout=1)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS chart_delivery (chat TEXT, fingerprint TEXT, token TEXT, sent INTEGER NOT NULL, at INTEGER, PRIMARY KEY(chat,fingerprint))')
            if 'at' not in {row[1] for row in db.execute('PRAGMA table_info(chart_delivery)')}:
                # Rows written before expiry existed start their retention now rather than expiring at once.
                db.execute('ALTER TABLE chart_delivery ADD COLUMN at INTEGER')
                db.execute('UPDATE chart_delivery SET at=?', (int(time.time()),))
            db.commit()
        except Exception:
            db.close()
            raise
        return db

    def expire(self, db, now):
        # Sent rows are kept for the retention period only; the same report may chart again after it.
        cutoff = now-self.retention
        db.execute('DELETE FROM chart_delivery WHERE sent=1 AND at<?', (cutoff,))
        for key in [k for k, at in self.sent.items() if at < cutoff]:
            del self.sent[key]

    def recover(self):
        # One plugin instance per data dir, like the existing parser lifecycle. Unfinished claims are discarded.
        if not self.path.exists():
            return
        db = self.connect()
        try:
            with db:
                db.execute('DELETE FROM chart_delivery WHERE sent=0')
                self.expire(db, int(time.time()))
        finally:
            db.close()

    def claim(self, chat, fingerprint):
        now = int(time.time())
        token = uuid.uuid4().hex
        db = self.connect()
        try:
            with db:
                self.expire(db, now)
                if (chat, fingerprint) in self.sent:
                    return None
                cursor = db.execute('INSERT OR IGNORE INTO chart_delivery VALUES (?,?,?,0,?)', (chat, fingerprint, token, now))
                return token if cursor.rowcount else None
        finally:
            db.close()

    def complete(self, chat, fingerprint, token):
        now = int(time.time())
        self.sent[(chat, fingerprint)] = now
        db = self.connect()
        try:
            with db:
                cursor = db.execute('UPDATE chart_delivery SET sent=1, at=? WHERE chat=? AND fingerprint=? AND token=?',
                                    (now, chat, fingerprint, token))
                if not cursor.rowcount:
                    raise RuntimeError('Chart delivery claim lost')
        finally:
            db.close()

    def release(self, chat, fingerprint, token):
        db = self.connect()
        try:
            with db:
                db.execute('DELETE FROM chart_delivery WHERE chat=? AND fingerprint=? AND token=? AND sent=0',
                           (chat, fingerprint, token))
        finally:
            db.close()


class ChartManager:
    def __init__(self, root):
        self.root = Path(root)
        self.ledger = DeliveryLedger(self.root/'chart_delivery.sqlite3')
        self.cleaners = set()
        self.processes = set()
        self.retained = set()

    async def initialize(self):
        try:
            await asyncio.to_thread(self.ledger.recover)
            for directory in self.root.glob('chart-*'):
                if directory.is_dir():
                    await asyncio.to_thread(shutil.rmtree, directory, True)
        except Exception as exc:
            logger.warning('Spark chart startup cleanup failed: %s', type(exc).__name__)

    async def render(self, directory, timezone, enabled, timeout=30):
        if not enabled():
            return False
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            logger.warning('Spark chart timezone invalid; using UTC')
            timezone = 'UTC'
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name('chart_worker.py')), str(directory), timezone,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.wait_for(process.wait(), 5)
            raise
        self.processes.add(process)
        waiter = asyncio.create_task(process.wait())
        try:
            started = time.monotonic()
            while process.returncode is None:
                if not enabled():
                    return False
                if time.monotonic()-started >= timeout:
                    raise TimeoutError('Chart rendering timed out')
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), .2)
                except asyncio.TimeoutError:
                    continue
            status = directory/'render.json'
            if process.returncode or not status.exists() or status.stat().st_size > 1024:
                raise RuntimeError('Chart worker failed')
            result = json.loads(status.read_text(encoding='utf-8'))
            if not result.get('ready'):
                raise RuntimeError('Chart renderer failed: '+str(result.get('error'))[:40])
            target = directory/'chart.png'
            if not target.is_file() or not 24 <= target.stat().st_size <= 8*1024*1024:
                raise ValueError('Invalid chart file size')
            with target.open('rb') as handle:
                header = handle.read(24)
            if header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR' or struct.unpack('>II', header[16:24]) != (2400, 1500):
                raise ValueError('Invalid chart PNG')
            return True
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.wait_for(process.wait(), 5)
            await waiter
            self.processes.discard(process)

    async def deliver(self, event, session, enabled, timezone='UTC'):
        if not enabled() or not session or not session.overview or 'error' in session.overview:
            return
        fingerprint = session.overview.get('fingerprint')
        if not fingerprint:
            return
        chat = hashlib.sha256(event.unified_msg_origin.encode()).hexdigest()
        token = directory = None
        delivered = False
        phase = '首次记录'
        try:
            claim = asyncio.create_task(asyncio.to_thread(self.ledger.claim, chat, fingerprint))
            try:
                token = await asyncio.shield(claim)
            except asyncio.CancelledError:
                token = await claim
                raise
            if not token or not enabled():
                return
            directory = Path(tempfile.mkdtemp(prefix='chart-', dir=self.root))
            phase = '数据导出'
            await session.export_telemetry(directory/'telemetry.json')
            # No model queries remain. Release parser memory before importing plotting libraries.
            await session.close()
            phase = '绘制'
            if not enabled() or not await self.render(directory, timezone, enabled):
                return
            if not enabled():
                return
            phase = '图片发送'
            await event.send(event.image_result(str(directory/'chart.png')))
            delivered = True
            phase = '首次记录保存'
            complete = asyncio.create_task(asyncio.to_thread(self.ledger.complete, chat, fingerprint, token))
            try:
                await asyncio.shield(complete)
            except asyncio.CancelledError:
                await complete
                raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('Spark chart %s failed: %s', phase, type(exc).__name__)
            if enabled():
                try:
                    message = '图表已发送，但首次记录保存失败；重启后可能再次出图。' if delivered else f'图表未完成（{phase}阶段），可再次发送报告重试；文字分析不受影响。'
                    await event.send(event.plain_result(message))
                except Exception as send_error:
                    logger.warning('Spark chart failure notice failed: %s', type(send_error).__name__)
        finally:
            if token and not delivered:
                try:
                    await asyncio.to_thread(self.ledger.release, chat, fingerprint, token)
                except Exception as exc:
                    logger.warning('Spark chart claim release failed: %s', type(exc).__name__)
            if directory:
                if delivered:
                    # Some adapters read image paths after send() returns. Retain only PNG briefly, not data.
                    for path in directory.iterdir():
                        if path.is_dir():
                            await asyncio.to_thread(shutil.rmtree, path, True)
                        elif path.name != 'chart.png':
                            path.unlink(missing_ok=True)
                    task = asyncio.create_task(self.expire(directory))
                    self.retained.add(directory)
                    self.cleaners.add(task)
                    task.add_done_callback(self.cleaners.discard)
                else:
                    await asyncio.to_thread(shutil.rmtree, directory, True)

    async def expire(self, directory):
        try:
            await asyncio.sleep(600)
        finally:
            await asyncio.to_thread(shutil.rmtree, directory, True)
            self.retained.discard(directory)

    async def close(self):
        for task in list(self.cleaners):
            task.cancel()
        await asyncio.gather(*self.cleaners, return_exceptions=True)
        for directory in list(self.retained):
            await asyncio.to_thread(shutil.rmtree, directory, True)
        self.retained.clear()
        for process in list(self.processes):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.wait_for(process.wait(), 5)
