"""Shared text/photo editor for new drafts and revision-checked scheduled posts."""
import asyncio
from .domain import Product, COPY_TEXT_LIMIT, units, validate_caption
from .editing import synchronize
from .card import clean_photo, render_card
from .storage import Stale


class EditorScreens:
    @staticmethod
    def content(record):
        return {key: record.get(key, default) for key, default in (
            ('caption', ''), ('entities', []), ('photo', b''), ('photo_id', ''),
            ('template', False), ('source_photo', b''), ('product_data', {}))}

    async def write_content(self, content, keyboard, *, message_id=None, previous_media=False):
        """Retain the card when possible; replace it when its media type changes."""
        media = bool(content.get('photo') or content.get('photo_id'))
        caption, entities = content.get('caption', ''), content.get('entities', [])
        shown = caption if caption or media else '⚠️ Sin texto.\n⚠️ Sin imagen.'
        if message_id is not None and (media == previous_media or media):
            if media:
                sent = await self.replace_photo(message_id, {**content, 'caption': shown}, keyboard)
                if isinstance(sent, dict) and sent.get('photo'):
                    content['_sent_photo_id'] = sent['photo'][-1]['file_id']
            else:
                await self.replace_screen(message_id, shown, keyboard, entities)
            return message_id
        if media:
            sent = await self.api.photo(self.owner, content.get('photo_id') or content['photo'], shown, entities, keyboard)
        else:
            sent = await self.say(shown, keyboard, entities)
        if isinstance(sent, dict) and sent.get('photo') and media:
            content['_sent_photo_id'] = sent['photo'][-1]['file_id']
        if message_id is not None:
            await self.remove(message_id)
        return sent['message_id']

    def editor_controls(self, content, prefix):
        def b(label, action):
            return {'text': label, 'callback_data': prefix + action}
        copy = None
        caption = content.get('caption', '')
        if caption and units(caption) <= COPY_TEXT_LIMIT:
            copy = {'text': '📋 - Copiar', 'copy_text': {'text': caption}}
        media = bool(content.get('photo') or content.get('photo_id'))
        remove = b('➖ - Quitar foto', 'remove') if media else None
        change = b('📷 - Cambiar foto', 'change') if media and content.get('template') else None
        replace = b('🖼️ - Sustituir imagen' if media else '🖼️ - Añadir imagen', 'replace')
        rows = []
        if copy:
            rows.append([x for x in (copy, remove) if x])
            rows.append([x for x in (change, replace) if x])
        elif media:
            rows.append([x for x in (remove, change) if x])
            rows.append([replace])
        else:
            rows.append([replace])
        rows.append([b('❌ - Cerrar', 'close')])
        if not caption:
            instructions = '✏️ Para editar la publicación:\n\nEnvía el texto que quieres utilizar o elige una opción de imagen.'
        else:
            first = 'Pulsa «Copiar».' if copy else 'Selecciona el texto de la vista previa y elige «Copiar».'
            instructions = ('✏️ Para editar la publicación:\n\n1. ' + first + '\n'
                            '2. Toca la caja de mensaje y elige «Pegar».\n'
                            '3. Haz los cambios y pulsa en «Enviar».')
        return instructions, rows

    @staticmethod
    def image_request_controls(prefix):
        return [[{'text': '⬅️ - Volver', 'callback_data': prefix + 'back'},
                 {'text': '❌ - Cerrar', 'callback_data': prefix + 'close'}]]

    @staticmethod
    def conflict_controls(prefix):
        return [[{'text': label, 'callback_data': prefix + action}] for label, action in (
            ('💶 - Usar precios', 'prices'), ('🔢 - Usar descuento', 'discount'),
            ('✏️ - Mantener mi edición', 'keep'))]

    async def image_input(self, message, content, mode):
        chosen = message['photo'][-1]
        limit = self.config.integer('MAX_DOWNLOAD_BYTES')
        if chosen.get('file_size', 0) > limit:
            raise ValueError('La foto supera MAX_DOWNLOAD_BYTES; envía una más pequeña.')
        data = await self.api.download_photo(chosen['file_id'], limit)
        if len(data) > limit:
            raise ValueError('La foto supera el límite de descarga.')
        source = await asyncio.to_thread(clean_photo, data, self.config.integer('MAX_IMAGE_PIXELS'))
        template = mode == 'change' or (mode != 'replace' and content.get('template') and bool(content.get('photo') or content.get('photo_id')))
        result = dict(content)
        result.update(photo_id='', template=template, source_photo=source if template else b'')
        if template:
            product = Product(**content['product_data'])
            result['photo'] = await asyncio.to_thread(render_card, product, source, self.config)
            await self.resolve_notice_events('rendered')
        else:
            result['photo'] = source
        return result

    async def text_input(self, caption, entities, content, choice=None):
        product_data = content.get('product_data') or {'original_url': ''}
        product = Product(**product_data)
        result = synchronize(caption, entities, content['caption'], product,
                             template=bool(content.get('template')), footer=self.config['PUBLICATION_FOOTER'], choice=choice)
        if result.conflict:
            return None, result.conflict
        await self.resolve_notice_events('footer-verified')
        updated = {**content, 'caption': result.caption, 'entities': result.entities, 'product_data': result.product.to_dict()}
        validate_caption(updated['caption'], updated['entities'], has_photo=bool(updated['photo'] or updated['photo_id']))
        if (content.get('template') and content.get('source_photo')
                and (product.price, product.reference, product.discount, product.label, product.show_discount) !=
                    (result.product.price, result.product.reference, result.product.discount, result.product.label, result.product.show_discount)):
            updated['photo'] = await asyncio.to_thread(render_card, result.product, content['source_photo'], self.config)
            await self.resolve_notice_events('rendered')
            updated['photo_id'] = ''
        return updated, None

    async def draft_editor_action(self, query, edit_id, action):
        editor = self.store.editor(self.owner)
        preview = self.store.preview(self.owner, edit_id, editing=True)
        message_id = query['message']['message_id']
        if not editor or editor['preview_id'] != edit_id or editor['prompt'] != message_id:
            raise Stale()
        state = editor['state']
        if action in ('close', 'cancel'):
            self.store.cancel_edit(self.owner, edit_id, message_id)
            from .service import main_keyboard
            await self.api.call('editMessageReplyMarkup', chat_id=self.owner, message_id=preview['message_id'],
                                reply_markup={'inline_keyboard': main_keyboard(edit_id)})
            for msg in editor['aux']:
                await self.remove(msg)
            await self.resolve_notice_events('editor-closed', abandon=True)
        elif action == 'back' and state.get('mode') in ('change', 'replace'):
            state.pop('mode', None)
            self.store.editor_state(self.owner, state)
            text, keyboard = self.editor_controls(self.content(preview), f'e:{edit_id}:')
            await self.replace_screen(message_id, text, keyboard)
        elif action in ('change', 'replace'):
            if action == 'change' and not preview['template']:
                raise Stale()
            state['mode'] = action
            self.store.editor_state(self.owner, state)
            text = ('📷 Envía la nueva foto del producto. Se conservará la plantilla.' if action == 'change'
                    else '🖼️ Envía la imagen que quieres utilizar. No se aplicará la plantilla.')
            await self.replace_screen(message_id, text, self.image_request_controls(f'e:{edit_id}:'))
        elif action == 'remove':
            content = self.content(preview)
            content.update(photo=b'', photo_id='', template=False, source_photo=b'')
            await self.finish_draft_edit(content)
            await self.resolve_content_notices('image', content)
        elif action in ('prices', 'discount', 'keep') and state.get('pending'):
            pending = state['pending']
            content, _ = await self.text_input(pending['caption'], pending['entities'], self.content(preview), action)
            await self.finish_draft_edit(content)
            await self.resolve_content_notices('caption', content)
        else:
            raise Stale()
        await self.answer(query)

    async def receive_draft_edit(self, message, editor):
        preview = self.store.preview(self.owner, editor['preview_id'], editing=True)
        if message['message_id'] <= editor['prompt']:
            raise Stale('Ese mensaje es anterior a la edición actual. Envíalo de nuevo.')
        content = self.content(preview)
        if message.get('photo'):
            content = await self.image_input(message, content, editor['state'].get('mode'))
        else:
            caption, entities = message.get('text', ''), message.get('entities', [])
            content, conflict = await self.text_input(caption, entities, content)
            if conflict:
                state = {**editor['state'], 'pending': {'caption': caption, 'entities': entities}}
                self.store.editor_state(self.owner, state)
                await self.replace_screen(editor['prompt'],
                    f'⚠️ Los precios indican un {conflict[0]} % de descuento,\npero has escrito un {conflict[1]} %. ¿Qué quieres conservar?',
                    self.conflict_controls(f"e:{editor['preview_id']}:"))
                await self.remove(message['message_id'])
                return
        await self.finish_draft_edit(content)
        await self.resolve_content_notices('image' if message.get('photo') else 'caption', content)
        await self.remove(message['message_id'])

    async def finish_draft_edit(self, content):
        await self.send_preview(content['photo'], content['caption'], content['entities'], content['photo_id'],
                                template=content['template'], source_photo=content['source_photo'], product_data=content['product_data'])
