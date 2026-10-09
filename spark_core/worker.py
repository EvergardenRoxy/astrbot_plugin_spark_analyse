"""Private worker. File protocol avoids pipe deadlocks and large IPC responses."""
import json
import sys
import time
from pathlib import Path

# Executed as a file; do not depend on the plugin's dynamically assigned module name.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spark_core.profile import Profile


def run():
    if sys.platform != 'win32':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1536*1024*1024, 1536*1024*1024))
    directory = Path(sys.argv[1])
    try:
        raw_path = directory/'profile.bin'
        if raw_path.stat().st_size > 128*1024*1024:
            raise ValueError('报告超限')
        profile = Profile(raw_path.read_bytes())
        ready = directory/'ready.tmp'
        overview = profile.overview()
        overview['evidence_pack'] = profile.evidence_pack()
        encoded = json.dumps(overview, ensure_ascii=False)
        if len(encoded.encode('utf-8')) > 128*1024:
            raise ValueError('报告证据包超过128 KiB，拒绝发送无界上下文')
        ready.write_text(encoded, encoding='utf-8')
        ready.replace(directory/'ready.json')
        sequence = 0
        while not (directory/'stop').exists():
            request_path = directory/f'request-{sequence}.json'
            if not request_path.exists():
                time.sleep(0.05)
                continue
            try:
                request = json.loads(request_path.read_text(encoding='utf-8'))
                result = profile.query(**request)
            except Exception as exc:
                result = {'error': str(exc)[:200]}
            target = directory/f'response-{sequence}.json'
            temporary = directory/'response.tmp'
            temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            temporary.replace(target)
            sequence += 1
    except Exception as exc:
        (directory/'ready.json').write_text(json.dumps({'error': type(exc).__name__+': '+str(exc)[:200]}), encoding='utf-8')
        raise

if __name__ == '__main__':
    run()
