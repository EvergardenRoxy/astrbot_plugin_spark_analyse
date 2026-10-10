import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from spark_core.cache import ProfileCache
from spark_core.profile import ProfileError
from spark_core.session import ReportSession, LoadTimeout

class LoadTests(unittest.IsolatedAsyncioTestCase):
    async def load_with_cache(self, root, cache):
        async def fake_download(url, path, **kwargs): path.write_bytes(b'complete-download')
        process = AsyncMock()
        process.returncode = 0
        s = ReportSession(root, cache=cache)
        try:
            with patch('spark_core.session.download', fake_download), patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)), patch.object(s, 'wait_file', AsyncMock(return_value={'threads': []})):
                return await s.load('https://spark.lucko.me/SyntheticReport001')
        finally:
            await s.close()

    async def load_real_worker(self, root, raw, cache=None):
        async def fake_download(url, path, **kwargs): path.write_bytes(raw)
        s = ReportSession(root, parse_timeout=30, cache=cache)
        try:
            with patch('spark_core.session.download', fake_download):
                return await s.load('https://spark.lucko.me/SyntheticReport001')
        finally:
            await s.close()

    async def test_worker_rejection_reason_reaches_user(self):
        from test_core import sample
        d = sample()
        d.metadata.sampler_mode = 1
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ProfileError, '首版仅支持') as caught:
                await self.load_real_worker(root, d.SerializeToString())
        self.assertNotIn('ProfileError', str(caught.exception))

    async def test_undecodable_report_has_clean_message_and_leaves_no_cache(self):
        with tempfile.TemporaryDirectory() as root:
            cache = ProfileCache(Path(root)/'profiles.sqlite3')
            with self.assertRaisesRegex(ProfileError, r'^报告解析失败（DecodeError）$'):
                await self.load_real_worker(root, b'\xff\xff not a protobuf report', cache)
            self.assertFalse(cache.get('SyntheticReport001', Path(root)/'copy'))

    def test_worker_hides_messages_that_may_contain_paths(self):
        worker = Path(__file__).resolve().parents[1]/'spark_core'/'worker.py'
        with tempfile.TemporaryDirectory() as root:
            subprocess.run([sys.executable, str(worker), root], capture_output=True, timeout=30)
            error = json.loads((Path(root)/'ready.json').read_text(encoding='utf-8'))['error']
        # Exact match: OSError repr-escapes backslashes, so a path check would miss Windows leaks.
        self.assertEqual(error, 'FileNotFoundError: ')

    async def test_unreadable_cache_database_falls_back_to_download(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'profiles.sqlite3'
            path.write_bytes(b'this is not a sqlite database'*50)
            self.assertEqual(await self.load_with_cache(root, ProfileCache(path)), {'threads': []})

    async def test_cache_write_failure_does_not_fail_analysis(self):
        with tempfile.TemporaryDirectory() as root:
            cache = ProfileCache(Path(root)/'profiles.sqlite3')
            with patch.object(cache, 'put', side_effect=sqlite3.OperationalError('database is locked')):
                self.assertEqual(await self.load_with_cache(root, cache), {'threads': []})

    async def test_download_timeout_is_labeled(self):
        with tempfile.TemporaryDirectory() as root:
            s = ReportSession(root)
            try:
                with patch('spark_core.session.download', AsyncMock(side_effect=TimeoutError())):
                    with self.assertRaisesRegex(LoadTimeout, '下载阶段'):
                        await s.load('https://spark.lucko.me/SyntheticReport001')
                self.assertIsNone(s.process)
            finally:
                await s.close()

    async def test_cache_reused_after_parse_timeout(self):
        from pathlib import Path
        from spark_core.cache import ProfileCache
        with tempfile.TemporaryDirectory() as root:
            cache = ProfileCache(Path(root)/'profiles.sqlite3')
            downloads = []
            async def fake_download(url, path, **kwargs):
                downloads.append(url)
                path.write_bytes(b'complete-download')
            process = AsyncMock()
            process.returncode = 0
            for _ in range(2):
                s = ReportSession(root, cache=cache)
                try:
                    with patch('spark_core.session.download', fake_download), patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)), patch.object(s, 'wait_file', AsyncMock(side_effect=TimeoutError())):
                        with self.assertRaises(LoadTimeout): await s.load('https://spark.lucko.me/SyntheticReport001')
                finally:
                    await s.close()
            self.assertEqual(len(downloads), 1)

    async def test_parse_timeout_is_labeled(self):
        with tempfile.TemporaryDirectory() as root:
            s = ReportSession(root)
            async def fake_download(url, path, **kwargs): path.write_bytes(b'data')
            process = AsyncMock()
            process.returncode = 0
            try:
                with patch('spark_core.session.download', fake_download), patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)), patch.object(s, 'wait_file', AsyncMock(side_effect=TimeoutError())):
                    with self.assertRaisesRegex(LoadTimeout, '解析/证据包'):
                        await s.load('https://spark.lucko.me/SyntheticReport001')
            finally:
                await s.close()

    async def test_close_removes_directory_when_worker_wait_fails(self):
        with tempfile.TemporaryDirectory() as root:
            s = ReportSession(root)
            (s.directory/'profile.bin').write_bytes(b'decoded report data')
            process = AsyncMock()
            process.returncode = None
            process.kill = lambda: None
            process.wait.side_effect = TimeoutError()
            s.process = process
            with self.assertRaises(TimeoutError):
                await s.close()
            self.assertFalse(s.directory.exists())


class WorkerLifecycleTests(unittest.TestCase):
    def test_worker_exits_when_directory_removed(self):
        # A host killed without cleanup leaves the worker running; removing its directory must stop it.
        import shutil, time
        from test_core import sample
        worker = Path(__file__).resolve().parents[1]/'spark_core'/'worker.py'
        root = tempfile.mkdtemp()
        directory = Path(root)/'spark-x'
        directory.mkdir()
        (directory/'profile.bin').write_bytes(sample().SerializeToString())
        proc = subprocess.Popen([sys.executable, str(worker), str(directory)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 30
            while not (directory/'ready.json').exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue((directory/'ready.json').exists())
            shutil.rmtree(directory)
            for _ in range(60):
                if proc.poll() is not None: break
                time.sleep(0.05)
            self.assertIsNotNone(proc.poll(), 'orphaned worker kept running after its directory was removed')
        finally:
            if proc.poll() is None: proc.kill()
            proc.wait()
            shutil.rmtree(root, ignore_errors=True)


class WorkerImportPathTests(unittest.TestCase):
    HERE = os.path.realpath(os.path.join(tempfile.gettempdir(), 'plugin', 'spark_core'))

    def test_script_directory_is_replaced(self):
        # Keeps spark_core/profile.py from shadowing the stdlib `profile` module.
        from spark_core.worker import import_path
        root = os.path.dirname(self.HERE)
        self.assertEqual(import_path([self.HERE, '/usr/lib/python3.zip'], self.HERE), [root, '/usr/lib/python3.zip'])

    def test_safe_path_entries_are_kept(self):
        # PYTHONSAFEPATH / -P / a ._pth file: entry 0 is PYTHONPATH or the stdlib zip, not the script directory.
        from spark_core.worker import import_path
        root = os.path.dirname(self.HERE)
        self.assertEqual(import_path(['/deps', '/usr/lib/python3.zip'], self.HERE), [root, '/deps', '/usr/lib/python3.zip'])

    @unittest.skipUnless(sys.version_info >= (3, 11), '-P needs Python 3.11+')
    def test_worker_runs_with_dependencies_only_on_pythonpath(self):
        # -S drops site-packages and -P drops the script directory, so protobuf is reachable only through
        # PYTHONPATH, which is then sys.path[0]. Overwriting that entry made the worker fail to import.
        import shutil, site, time
        from test_core import sample
        worker = Path(__file__).resolve().parents[1]/'spark_core'/'worker.py'
        paths = [*site.getsitepackages(), site.getusersitepackages()]
        root = tempfile.mkdtemp()
        (Path(root)/'profile.bin').write_bytes(sample().SerializeToString())
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(paths))
        env.pop('PYTHONSAFEPATH', None)
        proc = subprocess.Popen([sys.executable, '-S', '-P', str(worker), root], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 30
            while not (Path(root)/'ready.json').exists() and proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue((Path(root)/'ready.json').exists(), 'worker exited before writing ready.json')
            self.assertNotIn('error', json.loads((Path(root)/'ready.json').read_text(encoding='utf-8')))
        finally:
            shutil.rmtree(root, ignore_errors=True)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
