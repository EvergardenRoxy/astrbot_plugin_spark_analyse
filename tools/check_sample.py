"""Explicit network smoke test; never stores the reference profile in the repository.

Run outside AstrBot with the test logger stub on the path:
    PYTHONPATH=tests python tools/check_sample.py https://spark.lucko.me/<report-id>
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spark_core.session import ReportSession

async def run():
    with tempfile.TemporaryDirectory() as root:
        s = ReportSession(root)
        try:
            if len(sys.argv) != 2:
                raise SystemExit('请显式提供你获授权检查的报告URL；没有默认报告。')
            overview = await s.load(sys.argv[1])
            runtime = overview['runtime']
            print(json.dumps({'platform': overview['platform'], 'java': runtime['system']['java'],
                              'heap': runtime['heap'], 'platform_gc': runtime['platform_gc'],
                              'system_gc': runtime['system_gc'], 'server_hint': runtime['server_hint']}, ensure_ascii=False))
            t = next((x['id'] for x in overview['threads'] if x['name'] == 'Server thread'), 0)
            result = await s.query(thread=t)
            print(json.dumps({'thread': t, 'denominator_ms': result.get('denominator_ms'), 'rows': len(result.get('rows', [])), 'error': result.get('error')}, ensure_ascii=False))
            if 'error' in result: raise RuntimeError(result['error'])
        finally:
            await s.close()

asyncio.run(run())
