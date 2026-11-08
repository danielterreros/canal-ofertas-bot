"""Context-bound notices; displaying or acknowledging one never changes a publication."""
from dataclasses import dataclass
from .network import NetworkError
from .telegram import TelegramError


@dataclass(frozen=True)
class NoticeContext:
    scope: str
    operation: str
    field: str = ''
    input_id: int | None = None


def operation_event(context):
    suffix = ':' + context.field if context.operation == '/editar' and context.field else ''
    return 'operation:' + context.operation + suffix


def context_for(store, owner, message, query=None):
    try:
        scope = 'draft:' + store.draft(owner, live=False)['id']
    except ValueError:
        scope = 'session'
    text = message.get('text', '')
    command, _, value = text.partition(' ')
    operation = command.split('@')[0].lower() if text.startswith('/') else 'input'
    field = value.strip().partition(' ')[0] if operation == '/editar' else ''
    field = {'tipo': 'oferta', 'campana': 'campaña'}.get(field, field)
    if operation == '/anular':
        target = value.strip()
        scope = 'post:' + target if target and ' ' not in target else 'session'
    if query:
        parts = query.get('data', '').split(':')
        operation = ':'.join([parts[0], parts[2]]) if len(parts) > 2 else query.get('data', '')
        if parts[0] == 'n' and len(parts) > 1:
            scope = store.notice_scope(owner, parts[1]) or 'notice:' + parts[1]
        if parts[0] in ('q', 'r') and len(parts) > 1:
            try:
                view = store.scheduled_view(owner, parts[1], message.get('message_id'))
                scope = 'post:' + view['post_id']
            except ValueError:
                scope = 'control:' + parts[1]
    elif operation == 'input' or (operation == '/editar' and field == 'texto'):
        editor, scheduled = store.editor(owner), store.scheduled_editor(owner)
        if scheduled and (not editor or scheduled['message_id'] > editor['prompt']):
            scope = 'post:' + scheduled['post_id']
        if operation == 'input':
            operation = 'image' if message.get('photo') else 'caption' if editor or scheduled else 'prepare'
    return NoticeContext(scope, operation, field, None if query else message.get('message_id'))


def error_rule(text, context):
    """Give stable existing error messages a cleanup policy; unknown errors stay conservative."""
    operation, field = context.operation, context.field
    correction = 'field:' + field if operation == '/editar' and field and field != 'texto' else 'caption'
    if text.startswith('Comando no reconocido'):
        return 'session', 'unknown-command', ['recognized'], 'temporary'
    if text.startswith('No hay borrador activo'):
        return 'session', 'missing-draft', ['new-draft'], 'temporary'
    if text.startswith('No existe una publicación pendiente'):
        return context.scope, 'unknown-post', ['schedule-list'], 'temporary'
    if text.startswith('La oferta ha caducado'):
        return context.scope, 'expired-draft', ['new-draft'], 'temporary'
    if text.startswith('La cancelación está guardada'):
        return context.scope, 'cancel-update', ['cancel-updated'], 'verify'
    if text.startswith(('Publicación desactivada',)):
        return context.scope, 'publishing-disabled', ['scheduled', 'published'], 'verify'
    if text.startswith(('El destino', 'La zona horaria')):
        return context.scope, 'configuration-changed', ['preview', 'scheduled-open', 'scheduled'], 'verify'
    if text.startswith(('PUBLICATION_FOOTER', 'El enlace de PUBLICATION_FOOTER')):
        return context.scope, 'footer-configuration', ['footer-verified'], 'verify'
    if text.startswith(('Faltan las fuentes', 'La marca o etiqueta')):
        return context.scope, 'render-configuration', ['rendered'], 'verify'
    if text.startswith(('La foto', 'Foto demasiado', 'Telegram no devolvió una ruta de foto', 'No se pudo descargar la foto', 'La dirección no devuelve una foto')):
        return context.scope, 'image', ['image'], 'temporary'
    if text.startswith(('Introduce un precio', 'El importe debe', 'El título manual', 'El emoji admite', 'Las condiciones manuales', 'Etiqueta no reconocida', 'Se admiten hasta cinco')):
        return context.scope, correction, [correction], 'temporary'
    if text.startswith('La referencia debe'):
        return context.scope, 'reference-order', ['reference-valid'], 'temporary'
    if text.startswith('El porcentaje debe'):
        return context.scope, 'percentage', ['caption'], 'temporary'
    if text.startswith('No hay precio anterior para calcular'):
        return context.scope, 'percentage-reference', ['caption', 'reference-available'], 'temporary'
    if text.startswith(('La publicación debe', 'El formato del mensaje', 'Hay formatos solapados', 'El enlace y los datos de la oferta')):
        return context.scope, 'caption', ['caption', 'preview'], 'temporary'
    if text.startswith(('Faltan:', 'Usa /previa antes')):
        return context.scope, 'incomplete-draft', ['preview'], 'temporary'
    if text.startswith(('Uso: /editar', 'Campo desconocido')):
        return context.scope, 'edit-usage', ['command:/editar'], 'temporary'
    if text.startswith('Uso: /anular'):
        return context.scope, 'cancel-usage', ['command:/anular'], 'temporary'
    if (text.startswith(('Uso: /programar', 'Esa hora', 'Elige una fecha', 'Solo se puede programar', 'Hora o minuto',
                         'Elige un mes', 'Elige un día', 'No quedan minutos', 'Elige primero', 'La hora elegida', 'Selecciona de nuevo una fecha'))):
        return context.scope, 'schedule-date', ['scheduled', 'schedule-abandoned'], 'temporary'
    if text.startswith(('Esta vista previa', '⚠️ Esta programación', 'Este selector', 'El control no corresponde',
                        'Ese mensaje es anterior', 'Ya hay una edición', 'Usa los controles del selector')):
        return context.scope, 'stale-controls', ['preview', 'caption', 'editor-closed', 'scheduled-open', 'scheduled'], 'temporary'
    if text.startswith(('✅ Esta publicación', '⏳ Esta publicación')):
        return context.scope, 'post-state', ['scheduled-open', 'schedule-list'], 'temporary'
    event = operation_event(context)
    return context.scope, event, [event], 'verify'


def current_product_warnings(product, complete):
    """Remove fulfilled extraction instructions while retaining commercial/provenance warnings."""
    extraction = ('Amazon solicita CAPTCHA', 'Extracción HTML desactivada', 'Puedes completar todos',
                  'No hay un precio EUR', 'La ficha es demasiado compleja', 'Se agotó el tiempo', 'Amazon devolvió HTTP',
                  'El servidor no indica', 'El acortador no termina', 'El enlace no termina', 'El enlace tiene demasiadas',
                  'La respuesta no es una ficha', 'La respuesta excede', 'La respuesta descomprimida',
                  'Respuesta comprimida', 'Compresión remota', 'Conexión interrumpida',
                  'Solo se permiten conexiones', 'La dirección remota')
    result = []
    for warning in product.warnings:
        if complete and warning.startswith(extraction):
            continue
        if warning.startswith('Sin referencia fiable') and product.reference:
            warning = 'Referencia aportada manualmente; no acredita el mínimo de 30 días.'
        if warning.startswith('El precio de referencia puede ser PVP') and not product.reference:
            continue
        result.append(warning)
    return result


class NoticeManager:
    def __init__(self, store, api):
        self.store, self.api = store, api

    async def show(self, owner, scope, issue, text, recover=(), *, kind='temporary', input_id=None):
        if kind == 'critical' and input_id is None and self.store.existing_notice(owner, scope, issue):
            return
        fresh = self.store.add_notice(owner, scope, issue, recover, kind, input_id)
        keyboard = [[{'text': '✅ - Revisado', 'callback_data': f'n:{fresh}:review'}]] if kind == 'critical' else None
        if keyboard:
            sent = await self.api.message(owner, text, keyboard)
        else:
            sent = await self.api.message(owner, text)
        self.store.bind_notice(owner, fresh, sent['message_id'])
        await self.cleanup((owner,))

    async def error(self, owner, text, context):
        scope, issue, recover, kind = error_rule(text, context)
        await self.show(owner, scope, issue, text, recover, kind=kind, input_id=context.input_id)

    async def resolve(self, owner, scope, *events, abandon=False):
        self.store.resolve_notices(owner, scope, events, abandon=abandon)
        await self.cleanup((owner,))

    async def resolve_event(self, owner, event):
        self.store.resolve_notice_event(owner, event)
        await self.cleanup((owner,))

    async def cleanup(self, owners):
        for row in self.store.notice_deletions(owners):
            try:
                await self.api.call('deleteMessage', chat_id=row['owner'], message_id=row['message_id'])
            except TelegramError as exc:
                if exc.code not in (400, 403):
                    self.store.defer_notice_deletion(row['owner'], row['message_id'], exc.retry_after or 30)
                    break
            except NetworkError:
                self.store.defer_notice_deletion(row['owner'], row['message_id'], 30)
                break
            self.store.notice_deleted(row['owner'], row['message_id'])


class NoticeScreens:
    async def resolve_notice_events(self, *events, scope=None, abandon=False):
        context = self._notice_context.get()
        await self.notices.resolve(self.owner, scope or context.scope, *events, abandon=abandon)

    def current_notice_product(self):
        from .domain import Product
        draft = self.store.draft(self.owner)
        saved = self.store.saved_preview(self.owner)
        return Product(**(saved['product_data'] if saved and saved['product_data'] else draft['data']))

    async def resolve_content_notices(self, kind, content=None):
        events = [kind, operation_event(self._notice_context.get())]
        try:
            if content and content.get("product_data"):
                from .domain import Product
                p = Product(**content["product_data"])
            else:
                p = self.current_notice_product()
            if kind in ('caption', 'field:precio', 'field:anterior'):
                if p.reference and p.price:
                    from decimal import Decimal
                    if Decimal(p.reference) > Decimal(p.price):
                        events.append('reference-valid')
                    events.append('reference-available')
                elif not p.reference:
                    events.append('reference-valid')
            if kind == 'caption':
                if p.price:
                    events.append('field:precio')
                if p.reference:
                    events.append('field:anterior')
                if p.title and len(p.title) <= 110:
                    events.append('field:titulo')
        except ValueError:
            pass
        await self.resolve_notice_events(*events)
