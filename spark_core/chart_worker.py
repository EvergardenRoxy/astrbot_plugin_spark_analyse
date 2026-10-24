"""Supervised offline rendering process. stdout/stderr are private to the host."""
import json
import os
import sys
from pathlib import Path

if __name__ == '__main__':
    here = str(Path(__file__).resolve().parent)
    root = str(Path(here).parent)
    # Replace the script directory, so spark_core/profile.py cannot shadow stdlib profile.
    if sys.path and os.path.realpath(sys.path[0] or os.curdir) == here:
        sys.path[:] = [root, *sys.path[1:]]
    else:
        sys.path[:] = [root, *sys.path]


def run():
    directory = Path(sys.argv[1])
    try:
        # Scientific libraries otherwise reserve many thread stacks/large virtual mappings.
        os.environ['OPENBLAS_NUM_THREADS'] = '1'
        os.environ['OMP_NUM_THREADS'] = '1'
        os.environ['MPLCONFIGDIR'] = str(directory/'mpl')
        if sys.platform != 'win32':
            import resource
            from spark_core.worker import cap_memory
            status = cap_memory(resource)
            if status.startswith('unavailable'):
                raise RuntimeError('Render memory limit unavailable')
        from spark_core.telemetry import MAX_BUNDLE_BYTES
        source = directory/'telemetry.json'
        if source.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError('Chart data exceeds limit')
        data = json.loads(source.read_text(encoding='utf-8'))
        from spark_core.chart_render import render
        render(data, directory/'chart.png', sys.argv[2])
        result = {'ready': True}
    except Exception as exc:
        result = {'error': type(exc).__name__}
    temp = directory/'render.tmp'
    temp.write_text(json.dumps(result), encoding='utf-8')
    temp.replace(directory/'render.json')


if __name__ == '__main__':
    run()
