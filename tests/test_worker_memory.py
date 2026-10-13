"""The worker's address-space cap (review item B4 / known issue K2)."""
import json
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from spark_core.worker import MEMORY_KEY, MEMORY_LIMIT, cap_memory

WORKER = Path(__file__).resolve().parents[1]/'spark_core'/'worker.py'
INFINITY = -1


def fake_resource(soft, hard, error=None):
    calls = []
    def setrlimit(kind, value):
        if error:
            raise error
        calls.append(value)
    module = types.SimpleNamespace(RLIMIT_AS=9, RLIM_INFINITY=INFINITY, getrlimit=lambda kind: (soft, hard), setrlimit=setrlimit)
    return module, calls


class CapMemoryTests(unittest.TestCase):
    def test_no_inherited_limit_sets_the_plugin_cap(self):
        resource, calls = fake_resource(INFINITY, INFINITY)
        self.assertEqual(cap_memory(resource), 'set')
        self.assertEqual(calls, [(MEMORY_LIMIT, MEMORY_LIMIT)])

    def test_lower_inherited_limit_is_kept_instead_of_raised(self):
        # systemd LimitAS= / ulimit -v below 1.5 GiB: asking for 1.5 GiB would raise the hard limit and fail.
        lower = 1024*1024*1024
        for soft, hard in ((lower, lower), (lower, INFINITY), (INFINITY, lower), (lower, MEMORY_LIMIT*2)):
            with self.subTest(soft=soft, hard=hard):
                resource, calls = fake_resource(soft, hard)
                self.assertEqual(cap_memory(resource), f'inherited {lower}')
                self.assertEqual(calls, [(lower, lower)])

    def test_higher_inherited_limit_is_lowered_to_the_cap(self):
        resource, calls = fake_resource(MEMORY_LIMIT*4, MEMORY_LIMIT*4)
        self.assertEqual(cap_memory(resource), 'set')
        self.assertEqual(calls, [(MEMORY_LIMIT, MEMORY_LIMIT)])

    def test_refused_call_does_not_stop_the_worker(self):
        for error in (ValueError('not allowed to raise maximum limit'), OSError('unsupported')):
            with self.subTest(error=type(error).__name__):
                resource, _ = fake_resource(INFINITY, INFINITY, error)
                self.assertEqual(cap_memory(resource), 'unavailable: '+type(error).__name__)


@unittest.skipUnless(sys.platform.startswith('linux'), 'reads /proc and relies on Linux RLIMIT_AS semantics')
class RealWorkerTests(unittest.TestCase):
    def run_worker(self, inherited=None):
        """Start the real worker on a valid sample, optionally under an inherited hard limit; return
        (ready.json, the worker's address-space limit from /proc)."""
        from test_core import sample
        def limit():
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (inherited, inherited))
        root = tempfile.mkdtemp()
        (Path(root)/'profile.bin').write_bytes(sample().SerializeToString())
        proc = subprocess.Popen([sys.executable, str(WORKER), root], preexec_fn=limit if inherited else None,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time()+30
            while not (Path(root)/'ready.json').exists() and proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue((Path(root)/'ready.json').exists(), 'worker exited before writing ready.json')
            ready = json.loads((Path(root)/'ready.json').read_text(encoding='utf-8'))
            line = next(l for l in Path(f'/proc/{proc.pid}/limits').read_text().splitlines() if l.startswith('Max address space'))
            return ready, int(line.split()[3])
        finally:
            (Path(root)/'stop').touch()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            shutil.rmtree(root, ignore_errors=True)

    def test_worker_applies_the_cap(self):
        ready, hard = self.run_worker()
        self.assertNotIn('error', ready)
        self.assertEqual(ready[MEMORY_KEY], 'set')
        self.assertEqual(hard, MEMORY_LIMIT)

    def test_worker_parses_under_a_lower_inherited_limit(self):
        # The K2 reproduction (`ulimit -v 1200000`). Before the fix the worker died in setrlimit and the user only
        # saw "解析worker异常退出".
        inherited = 1200000*1024
        ready, hard = self.run_worker(inherited)
        self.assertNotIn('error', ready)
        self.assertIn('threads', ready)
        self.assertEqual(ready[MEMORY_KEY], f'inherited {inherited}')
        self.assertEqual(hard, inherited)


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def parse(self, ready):
        from spark_core.session import ReportSession
        process = AsyncMock()
        process.returncode = None
        process.pid = 1
        with tempfile.TemporaryDirectory() as root:
            session = ReportSession(root)
            try:
                with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)), \
                     patch.object(session, 'wait_file', AsyncMock(return_value=dict(ready))), \
                     patch('spark_core.session.logger') as logger:
                    overview = await session.parse()
            finally:
                session.process = None
                await session.close()
        return overview, logger

    async def test_status_is_logged_and_kept_out_of_the_overview(self):
        overview, logger = await self.parse({'threads': [], MEMORY_KEY: 'set'})
        self.assertNotIn(MEMORY_KEY, overview)
        self.assertIn('set', logger.info.call_args_list[-1].args)
        logger.warning.assert_not_called()

    async def test_missing_cap_is_a_warning(self):
        overview, logger = await self.parse({'threads': [], MEMORY_KEY: 'unavailable: OSError'})
        self.assertNotIn(MEMORY_KEY, overview)
        self.assertIn('unavailable: OSError', logger.warning.call_args.args)

    async def test_worker_error_still_reaches_the_user(self):
        from spark_core.profile import ProfileError
        with self.assertRaisesRegex(ProfileError, '^坏报告$'):
            await self.parse({'error': 'ProfileError: 坏报告', MEMORY_KEY: 'set'})
