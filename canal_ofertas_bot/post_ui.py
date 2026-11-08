"""Consistent private cards; their notices never become publication content."""
from .calendar_ui import when_short
from .editing import title_from_caption


def summary(record):
    caption = record.get('caption', '')
    lines = []
    if not caption.strip():
        lines.append('⚠️ Sin texto.')
    if not (record.get('photo') or record.get('photo_id')):
        lines.append('⚠️ Sin imagen.')
    if caption.strip():
        words = title_from_caption(caption).split()
        if words:
            lines.append(' '.join(words[:5]) + ('…' if len(words) > 5 else ''))
    return lines


def draft_card_text(record):
    return '\n'.join(['📝 Borrador', *summary(record)])


def scheduled_card_text(post, zone, state='pending'):
    heading = {'pending': '🕒 Pendiente', 'sent': '✅ Publicada', 'cancelled': '🚫 Cancelada'}[state]
    due = post.get('scheduled_due')
    if due is None:
        due = post['due']
    return '\n'.join([heading, when_short(due, zone), *summary(post),
                      f"Preparado por: {post['owner']}", f"Destino: {post['destination']}"])
