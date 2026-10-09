"""Build bundled protobuf classes from a pinned upstream revision (development only)."""
from pathlib import Path
import urllib.request
import subprocess
import sys

REV = '03210f75c3b040f1bf7b369761573b83c75bb5ce'
ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'spark_core' / 'proto'
DEST.mkdir(parents=True, exist_ok=True)
for name in ('spark.proto', 'spark_sampler.proto'):
    url = f'https://raw.githubusercontent.com/lucko/spark/{REV}/spark-common/src/main/proto/spark/{name}'
    content = urllib.request.urlopen(url, timeout=30).read()
    (DEST / name).write_bytes(content)
# Rewrite only the import path so generated modules remain plugin-relative.
p = DEST / 'spark_sampler.proto'
p.write_text(p.read_text().replace('"spark/spark.proto"', '"spark.proto"'))
subprocess.run([sys.executable, '-m', 'grpc_tools.protoc', f'-I{DEST}', f'--python_out={DEST}', str(DEST/'spark.proto'), str(DEST/'spark_sampler.proto')], check=True)
p = DEST / 'spark_sampler_pb2.py'
p.write_text(p.read_text().replace('import spark_pb2 as spark__pb2', 'from . import spark_pb2 as spark__pb2'))
license_url = f'https://raw.githubusercontent.com/lucko/spark/{REV}/LICENSE.txt'
try:
    license_data = urllib.request.urlopen(license_url, timeout=30).read()
except urllib.error.HTTPError:
    license_data = urllib.request.urlopen(f'https://raw.githubusercontent.com/lucko/spark/{REV}/LICENSE', timeout=30).read()
(ROOT/'LICENSE').write_bytes(license_data)
print('Generated official schema:', REV)
