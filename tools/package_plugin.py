from pathlib import Path
import re
import zipfile

root = Path(__file__).resolve().parents[1]
dest = root/'dist'
dest.mkdir(exist_ok=True)
files = [root/name for name in ('main.py', 'metadata.yaml', '_conf_schema.json', 'requirements.txt', 'analysis_policy.md', 'diagnosis_guide.md', 'reply_prompt.txt', 'README.md', 'LICENSE', 'THIRD_PARTY.md')]
files += [p for p in (root/'spark_core').rglob('*') if p.is_file() and '__pycache__' not in p.parts]
# metadata.yaml is the single source of the release version.
version = re.search(r'^version:\s*(\S+)', (root/'metadata.yaml').read_text(encoding='utf-8'), re.M).group(1).strip('\'"')
path = dest/f'astrbot_plugin_spark-{version}.zip'
with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
    for file in files:
        archive.write(file, 'astrbot_plugin_spark/'+file.relative_to(root).as_posix())
with zipfile.ZipFile(path) as archive:
    assert archive.testzip() is None
    assert not any('.venv' in n or n.endswith('.bin') for n in archive.namelist())
print(path, 'entries:', len(files))
