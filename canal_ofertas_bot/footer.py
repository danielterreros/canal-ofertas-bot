"""Optional publication signature; Markdown links become Telegram entities."""
import re
from urllib.parse import urlsplit
from .domain import units

LINK = re.compile(r'\[([^\]\n]+)\]\((https://[^\s()]+)\)')
MENTION = re.compile(r'(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}\b')


def parse_footer(value):
    text, entities, cursor = '', [], 0
    for match in LINK.finditer(value.strip()):
        text += value.strip()[cursor:match.start()]
        url = match[2]
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('El enlace de PUBLICATION_FOOTER debe ser una dirección HTTPS válida.')
        entities.append({'type': 'text_link', 'offset': units(text), 'length': units(match[1]), 'url': url})
        text += match[1]
        cursor = match.end()
    text += value.strip()[cursor:]
    if units(text) > 256:
        raise ValueError('PUBLICATION_FOOTER es demasiado larga para un único botón Copiar.')
    for match in MENTION.finditer(text):
        start, size = units(text[:match.start()]), units(match[0])
        if not any(e['offset'] < start + size and start < e['offset'] + e['length'] for e in entities):
            entities.append({'type': 'mention', 'offset': start, 'length': size})
    return text, sorted(entities, key=lambda e: e['offset'])


def restore_footer(text, entities, footer):
    """Restore an unchanged signature after plain clipboard copy, never reinsert it."""
    visible, links = parse_footer(footer)
    result = [dict(e) for e in entities]
    if visible and text.rstrip().endswith(visible):
        start = len(text.rstrip()) - len(visible)
        if start and text[start - 1] != '\n':
            return result
        offset = units(text[:start])
        for link in links:
            item = {**link, 'offset': link['offset'] + offset}
            if not any(e['offset'] < item['offset'] + item['length'] and item['offset'] < e['offset'] + e['length']
                       and e['type'] in ('text_link', 'mention', 'url') for e in result):
                result.append(item)
    return sorted(result, key=lambda e: e['offset'])
