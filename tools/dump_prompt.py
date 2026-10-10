"""Print what the analysis model receives for one report: the system prompt, then the user message.

Development only, for comparing prompt versions in any chat UI. Run outside AstrBot with the logger stub:
    PYTHONPATH=tests python tools/dump_prompt.py https://spark.lucko.me/<report-id> "用户的问题" > prompt.txt
Do not commit the output: it contains the report's data.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from spark_core.briefing import reply_requirements, system_prompt, user_payload
from spark_core.session import ReportSession


async def run(url, question):
    with tempfile.TemporaryDirectory() as root:
        session = ReportSession(root)
        try:
            overview = await session.load(url)
        finally:
            await session.close()
    read = lambda name: (ROOT/name).read_text(encoding='utf-8')
    print('===== SYSTEM PROMPT =====')
    print(system_prompt(read('analysis_policy.md'), read('diagnosis_guide.md'),
                        reply_requirements('', read('reply_prompt.txt'))))
    print('===== USER MESSAGE =====')
    # The plugin passes the whole chat message, link included.
    print(user_payload((url+' '+question).strip(), overview))


if __name__ == '__main__':
    if len(sys.argv) not in (2, 3):
        raise SystemExit('用法：PYTHONPATH=tests python tools/dump_prompt.py <报告链接> [用户问题]')
    asyncio.run(run(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else ''))
