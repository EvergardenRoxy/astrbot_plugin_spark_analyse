"""Private worker. File protocol avoids pipe deadlocks and large IPC responses."""
import json
import os
import sys
import time
from pathlib import Path


def import_path(entries, here):
    """sys.path with the plugin root first, so `spark_core` imports without the plugin's dynamic package name.

    Python normally puts the script directory first: replace it so spark_core/profile.py cannot shadow stdlib
    `profile`. Under PYTHONSAFEPATH, -P or a ._pth file there is no script directory and entry 0 is a
    PYTHONPATH entry or the stdlib zip, so every entry is kept.
    """
    root = os.path.dirname(here)
    if entries and os.path.realpath(entries[0] or os.curdir) == here:
        return [root, *entries[1:]]
    return [root, *entries]


# Only when run as the worker script; importing this module (tests) must not touch the caller's sys.path.
if __name__ == '__main__':
    sys.path[:] = import_path(sys.path, os.path.dirname(os.path.realpath(__file__)))
from spark_core.profile import Profile, ProfileError
from spark_core.observable import ObservableProfile, OBSERVABLE_DECODED_LIMIT

MEMORY_LIMIT = 1536*1024*1024
# Key the host removes from ready.json and logs; it is not part of the overview.
MEMORY_KEY = '_worker_memory_limit'


def cap_memory(resource):
    """Cap the address space at MEMORY_LIMIT, or at a lower limit already inherited; returns what happened.

    An unprivileged process cannot raise its hard limit, so asking for 1.5 GiB under systemd `LimitAS=` or
    `ulimit -v` below that used to kill the worker before it could report anything. The stricter limit is kept
    instead. If the platform refuses the call, parsing continues without a cap and the host logs it.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = min([MEMORY_LIMIT, *(v for v in (soft, hard) if v != resource.RLIM_INFINITY and v >= 0)])
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError) as exc:
        return 'unavailable: '+type(exc).__name__
    return 'set' if limit == MEMORY_LIMIT else f'inherited {limit}'


def run():
    parent = os.getppid()
    if sys.platform == 'win32':
        memory = 'unsupported'
    else:
        import resource
        memory = cap_memory(resource)
    directory = Path(sys.argv[1])
    observable = len(sys.argv) > 2 and sys.argv[2] == 'observable'
    try:
        raw_path = directory/'profile.bin'
        if observable and raw_path.stat().st_size > OBSERVABLE_DECODED_LIMIT:
            raise ProfileError('报告超限：Observable 报告解压后超过 48 MiB，通常是采样时间太长或实体过多。请缩短采样时间后重新采集')
        if raw_path.stat().st_size > 128*1024*1024:
            raise ProfileError('报告超限：报告解压后超过 128 MiB，通常是采样时间太长。请缩短采样时间后重新上传，例如 /spark profiler start --timeout 300（5 分钟后自动停止）')
        profile = (ObservableProfile if observable else Profile)(raw_path.read_bytes())
        ready = directory/'ready.tmp'
        overview = profile.overview()
        if not observable:
            overview['evidence_pack'] = profile.evidence_pack()
        encoded = json.dumps(overview, ensure_ascii=False)
        if len(encoded.encode('utf-8')) > 128*1024:
            raise ProfileError('报告证据包超过128 KiB，拒绝发送无界上下文')
        ready.write_text(json.dumps({**overview, MEMORY_KEY: memory}, ensure_ascii=False), encoding='utf-8')
        ready.replace(directory/'ready.json')
        sequence = 0
        while not (directory/'stop').exists():
            # Exit once orphaned: the host died without cleanup, or swept the directory on restart.
            # getppid() does not change on Windows, so the directory check is the portable signal.
            if not directory.exists() or (sys.platform != 'win32' and os.getppid() != parent):
                return
            request_path = directory/f'request-{sequence}.json'
            if not request_path.exists():
                time.sleep(0.05)
                continue
            try:
                request = json.loads(request_path.read_text(encoding='utf-8'))
                if request == {'op': 'telemetry_export'} and not observable:
                    from spark_core.telemetry import export
                    bundle = export(profile)
                    temp = directory/'telemetry.tmp'
                    temp.write_text(json.dumps(bundle, ensure_ascii=False, allow_nan=False), encoding='utf-8')
                    temp.replace(directory/'telemetry.json')
                    result = {'schema_version': 1, 'ready': True}
                else:
                    result = profile.query(**request)
            except Exception as exc:
                result = {'error': str(exc)[:200]}
            target = directory/f'response-{sequence}.json'
            temporary = directory/'response.tmp'
            temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            temporary.replace(target)
            sequence += 1
    except Exception as exc:
        # Chat shows only plugin-authored ProfileError text; other messages can carry local paths.
        reason = str(exc)[:200] if isinstance(exc, ProfileError) else ''
        failed = directory/'ready.tmp'
        failed.write_text(json.dumps({'error': type(exc).__name__+': '+reason, MEMORY_KEY: memory}), encoding='utf-8')
        failed.replace(directory/'ready.json')
        raise

if __name__ == '__main__':
    run()
