"""Plain-text delivery without another LLM call or loss of table values."""
import re


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
