import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from spark_core.profile import Profile, ProfileError
from spark_core.session import ReportSession
from spark_core.tasks import cancel_bounded
from test_core import sample

class ReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_timeout_invalidates_session(self):
        with tempfile.TemporaryDirectory() as root:
            s = ReportSession(root)
            try:
                with patch.object(s, 'wait_file', AsyncMock(side_effect=TimeoutError())):
                    with self.assertRaises(TimeoutError): await s.query(thread=0)
                (s.directory/'response-0.json').write_text('{"stale":true}')
                with self.assertRaises(ProfileError): await s.query(thread=1)
            finally:
                await s.close()

    async def test_cancel_deadline(self):
        release = asyncio.Event()
        async def stubborn():
            try: await release.wait()
            except asyncio.CancelledError: await release.wait()
        task = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        remaining = await cancel_bounded({task}, timeout=0.01)
        self.assertIn(task, remaining)
        release.set()
        await task

    def test_oversized_runtime_is_marked(self):
        d = sample()
        d.metadata.system_statistics.java.vm_args = 'x'*1000000
        runtime = Profile(d.SerializeToString()).runtime_metadata()
        self.assertLess(len(json.dumps(runtime)), 30000)
        self.assertTrue(runtime['truncated_fields'])

    def test_invalid_root_rejected(self):
        d = sample()
        d.threads[0].times[0] = 1
        with self.assertRaises(ProfileError): Profile(d.SerializeToString())
