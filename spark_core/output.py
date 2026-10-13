"""Plain-text delivery without another LLM call or loss of table values."""
import re
from urllib.parse import urlsplit

# Report text reaches the analysis model, so a crafted report can make it write a phishing link or flood the chat
# in the bot's name. Links stay clickable only for the report sites; the reply is cut at a fixed length.
TRUSTED_HOSTS = frozenset({'spark.lucko.me', 'observable.tas.sh'})
MAX_REPLY_CHARS = 3000
# Brackets stay in a match so an IPv6 host (`http://[::1]/`) is checked too; a malformed one counts as untrusted.
URL = re.compile(r'(?:https?://|www\.)[^\s<>"\'()（）【】{}，。；！？、]+', re.I)
UNTRUSTED_NOTE = '（回复中的外部链接未经核实，已改为不可点击的形式）'
TRUNCATED_NOTE = '……（回复过长，已截断）'


def plain_text(text):
    lines = []
    headers = None
    for line in text.splitlines():
        if line.strip().startswith('```'):
            continue
        if line.strip().startswith('|') and line.strip().endswith('|'):
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if all(re.fullmatch(r':?-{3,}:?', c) for c in cells):
                continue
            if headers is None:
                headers = cells
            else:
                lines.append('；'.join(f'{headers[i] if i < len(headers) else "字段"}：{c}' for i, c in enumerate(cells)))
            continue
        headers = None
        line = re.sub(r'^\s{0,3}#{1,6}\s+', '', line)
        line = re.sub(r'^\s*>\s?', '', line)
        line = re.sub(r'^\s*[-*+]\s+', '• ', line)
        lines.append(line)
    result = '\n'.join(lines)
    result = re.sub(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)', r'\1（\2）', result)
    result = re.sub(r'\*\*(.*?)\*\*|__(.*?)__', lambda m: m.group(1) or m.group(2) or '', result)
    result = re.sub(r'`([^`]+)`', r'\1', result)
    return re.sub(r'\n{3,}', '\n\n', result).strip()


def _host(url):
    try:
        return urlsplit(url if '://' in url else 'http://'+url).hostname or ''
    except ValueError:
        return ''


def bound_reply(text, limit=MAX_REPLY_CHARS):
    """The model's reply as sent to chat: untrusted links defanged (`evil[.]example/x`), then cut to `limit` chars."""
    defanged = False
    def defang(match):
        nonlocal defanged
        url = match.group(0)
        if _host(url) in TRUSTED_HOSTS:
            return url
        defanged = True
        rest = url.split('://', 1)[-1]
        authority, slash, path = rest.partition('/')
        return authority.replace('.', '[.]')+slash+path
    text = URL.sub(defang, text)
    if len(text) > limit:
        text = text[:limit].rstrip()+TRUNCATED_NOTE
    return text+('\n'+UNTRUSTED_NOTE if defanged else '')
