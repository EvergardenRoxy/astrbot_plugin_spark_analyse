""".sparkprofile attachments: bounded fetch from the adapter's local file or URL, then the normal worker parse."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import aiohttp
from spark_core.cache import ProfileCache
from spark_core.profile import ProfileError
from spark_core.session import LoadTimeout, ReportSession
from spark_core.transport import fetch_file, is_report_file
from test_core import sample

SECRET_URL = 'https://files.example/download?token=secret-token'


class FileFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target = self.root/'profile.bin'

    def tearDown(self):
        self.temp.cleanup()

    def local(self, data, name='profile.sparkprofile'):
        path = self.root/name
        path.write_bytes(data)
        return str(path)

    async def fetch_url(self, payload, status=200, content_length=None, raises=None, **limits):
        seen = {}
        class Content:
            async def iter_chunked(self, size):
                if raises:
                    raise raises
                for offset in range(0, len(payload), 16):
                    yield payload[offset:offset+16]
        class Response:
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        Response.status = status
        Response.content_length = content_length
        class Session:
            def __init__(self, **kwargs): seen['session'] = kwargs
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def get(self, url, **kwargs):
                seen['url'], seen['get'] = url, kwargs
                return Response()
        with patch('spark_core.transport.aiohttp.ClientSession', Session):
            await fetch_file('', SECRET_URL, self.target, **limits)
        return seen

    def test_report_file_names(self):
        for name in ('profile.sparkprofile', 'PROFILE-2026-10-10.SparkProfile'):
            self.assertTrue(is_report_file(name))
        for name in ('profile.sparkheap', 'profile.sparkprofile.txt', 'notes.txt', '', None):
            self.assertFalse(is_report_file(name))

    async def test_local_file_is_copied(self):
        raw = sample().SerializeToString()
        await fetch_file(self.local(raw), '', self.target)
        self.assertEqual(self.target.read_bytes(), raw)

    async def test_file_uri_is_a_local_file(self):
        raw = sample().SerializeToString()
        await fetch_file(Path(self.local(raw)).as_uri(), '', self.target)
        self.assertEqual(self.target.read_bytes(), raw)

    async def test_local_file_is_preferred_over_url(self):
        with patch('spark_core.transport.aiohttp.ClientSession') as session:
            await fetch_file(self.local(b'data'), SECRET_URL, self.target)
        session.assert_not_called()

    async def test_oversized_and_empty_files_rejected(self):
        with self.assertRaisesRegex(ProfileError, '超过 128 MiB.*--timeout 300'):
            await fetch_file(self.local(b'x'*100), '', self.target, limit=10)
        with self.assertRaisesRegex(ProfileError, '为空'):
            await fetch_file(self.local(b''), '', self.target)

    async def test_missing_file_without_url_rejected(self):
        for local, url in (('', ''), (str(self.root/'gone.sparkprofile'), ''), ('', 'ftp://files.example/a'),
                           ('', 'file:///etc/hostname')):
            with self.subTest(local=local, url=url), patch('spark_core.transport.aiohttp.ClientSession') as session:
                with self.assertRaisesRegex(ProfileError, '无法读取聊天中的文件'):
                    await fetch_file(local, url, self.target)
                session.assert_not_called()

    async def test_url_is_fetched_like_an_attachment(self):
        raw = sample().SerializeToString()
        seen = await self.fetch_url(raw)
        self.assertEqual(self.target.read_bytes(), raw)
        self.assertEqual(seen['url'], SECRET_URL)
        # Chat attachments are not Spark downloads: AstrBot's own proxy environment applies, not the plugin proxy.
        self.assertIs(seen['session']['trust_env'], True)
        self.assertNotIn('proxy', seen['get'])
        self.assertEqual(seen['get']['headers'], {'Accept-Encoding': 'identity'})
        self.assertNotIn('ssl', seen['get'])  # aiohttp default certificate verification

    async def test_missing_local_file_falls_back_to_url(self):
        seen = {}
        async def fetch(url, path, limit, timeout):
            seen['url'] = url
            path.write_bytes(b'data')
        with patch('spark_core.transport._fetch', fetch):
            await fetch_file(str(self.root/'gone.sparkprofile'), SECRET_URL, self.target)
        self.assertEqual(seen['url'], SECRET_URL)

    async def test_url_limits_and_status(self):
        with self.assertRaisesRegex(ProfileError, '超过 128 MiB'):
            await self.fetch_url(b'x'*100, limit=10)
        with self.assertRaisesRegex(ProfileError, '超过 128 MiB'):
            await self.fetch_url(b'', content_length=11, limit=10)
        with self.assertRaisesRegex(ProfileError, 'HTTP 404'):
            await self.fetch_url(b'x', status=404)
        with self.assertRaisesRegex(ProfileError, '为空'):
            await self.fetch_url(b'')

    async def test_network_errors_never_show_the_url(self):
        error = aiohttp.ClientConnectionError(f'Cannot connect to {SECRET_URL}')
        with self.assertRaisesRegex(ProfileError, r'^文件下载失败（ClientConnectionError），请重新发送文件$') as caught:
            await self.fetch_url(b'', raises=error)
        self.assertIsNone(caught.exception.__cause__)
        with self.assertRaisesRegex(TimeoutError, '阶段=读取响应体') as caught:
            await self.fetch_url(b'', raises=TimeoutError(SECRET_URL))
        self.assertNotIn('secret-token', str(caught.exception))


class FileSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_sparkprofile_file_is_parsed_by_the_worker(self):
        # spark --save-to-file writes SamplerData.toByteArray(); sample() serialises the same message.
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'profile-2026-10-10_12.00.00.sparkprofile'
            path.write_bytes(sample().SerializeToString())
            cache = ProfileCache(Path(root)/'profiles.sqlite3')
            s = ReportSession(root, parse_timeout=30, cache=cache)
            try:
                overview = await s.load_file(str(path), '')
                self.assertEqual(overview['platform']['name'], 'NeoForge')
                self.assertEqual(overview['threads'][0]['name'], 'Server thread')
                result = await s.query(thread=0)
                self.assertEqual(result['denominator_ms'], 300)
            finally:
                await s.close()
            # Attachments are not cached: the cache database is never even created.
            self.assertFalse((Path(root)/'profiles.sqlite3').exists())

    async def test_undecodable_file_has_clean_message(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'renamed.sparkprofile'
            path.write_bytes(b'\xff\xff not a protobuf report')
            s = ReportSession(root, parse_timeout=30)
            try:
                with self.assertRaisesRegex(ProfileError, r'^报告解析失败（DecodeError）$'):
                    await s.load_file(str(path), '')
            finally:
                await s.close()

    async def test_file_fetch_timeout_is_labeled(self):
        with tempfile.TemporaryDirectory() as root:
            s = ReportSession(root, download_timeout=45)
            try:
                with patch('spark_core.session.fetch_file', AsyncMock(side_effect=TimeoutError('阶段=读取响应体'))):
                    with self.assertRaisesRegex(LoadTimeout, r'文件获取阶段超时（45秒）；尚未调用分析模型；阶段=读取响应体'):
                        await s.load_file('', SECRET_URL)
                self.assertIsNone(s.process)
            finally:
                await s.close()


if __name__ == '__main__': unittest.main()
