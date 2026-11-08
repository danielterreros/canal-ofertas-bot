"""Revision-checked views of scheduled posts; edits remain tentative until Save."""
from datetime import datetime
from . import calendar_ui
from .post_ui import scheduled_card_text
from .network import NetworkError
from .storage import Stale
from .telegram import TelegramError


class ScheduledScreens:
    @staticmethod
    def scheduled_button(label, view, action):
        return {'text': label, 'callback_data': f'q:{view}:{action}'}

    async def invalidate_scheduled(self, post):
        for actor, message_id in self.store.scheduled_messages(post['id'], post['revision']):
            try:
                await self.api.call('editMessageReplyMarkup', chat_id=actor, message_id=message_id,
                                    reply_markup={'inline_keyboard': []})
            except (TelegramError, NetworkError):
                pass

    def current_confirmation_text(self, post):
        return scheduled_card_text(post, self.config.timezone)

    async def open_scheduled(self, post, message_id=None, *, previous_media=None):
        media = bool(post['photo'] or post['photo_id'])
        fresh = self.store.add_scheduled_view(self.owner, post['id'], post['revision'], 'preview',
                                             {'media': media}, self.schedule_scope())
        def b(label, action):
            return self.scheduled_button(label, fresh, action)
        keyboard = [[b('✅ - Publicar', 'publish'), b('🕒 - Reprogramar', 'schedule')],
                    [b('✏️ - Editar', 'edit'), b('🚫 - Cancelar', 'cancel')], [b('❌ - Cerrar', 'close')]]
        message_id = await self.write_content(self.content(post), keyboard, message_id=message_id,
                                              previous_media=media if previous_media is None else previous_media)
        self.store.bind_scheduled_view(self.owner, fresh, message_id)
        await self.resolve_notice_events('scheduled-open', scope='post:' + post['id'])
        return message_id

    async def scheduled_callback(self, query, view_token, action):
        message_id = query['message']['message_id']
        view = self.store.scheduled_view(self.owner, view_token, message_id)
        post = self.store.scheduled_post(view['post_id'], view['revision'], self.owner, self.schedule_scope())
        if post['destination'] != self.config.channel:
            raise Stale('El destino ha cambiado. Revisa la configuración antes de modificar esta programación.')
        state = view['state']
        if view['kind'] == 'editor':
            await self.scheduled_editor_action(query, view, post, action)
            return
        await self.answer(query)
        if action == 'close':
            if view['kind'] == 'preview':
                await self.schedule_confirmation(post['id'], message_id=message_id,
                                                  media=state.get('media', bool(post['photo'] or post['photo_id'])))
            elif view['kind'] == 'review':
                await self.open_scheduled(post, message_id=message_id, previous_media=state.get('media', bool(post['photo'] or post['photo_id'])))
            else:
                raise Stale()
            self.store.close_scheduled_view(self.owner, view_token)
        elif action == 'open' and view['kind'] in ('card', 'confirmation'):
            await self.open_scheduled(post, message_id=message_id, previous_media=state.get('media', bool(post['photo'] or post['photo_id'])))
            self.store.close_scheduled_view(self.owner, view_token)
        elif action == 'cancel' and view['kind'] in ('card', 'confirmation', 'preview'):
            previous = self.store.mutate_scheduled(self.owner, view_token, message_id, self.schedule_scope(), 'cancel')
            await self.invalidate_scheduled(previous)
            await self.delivery.notify()
        elif action == 'publish' and view['kind'] == 'preview':
            self.enabled()
            previous = self.store.mutate_scheduled(self.owner, view_token, message_id, self.schedule_scope(), 'publish')
            await self.invalidate_scheduled(previous)
            await self.delivery.send(post['id'])
            await self.delivery.notify()
        elif action == 'schedule' and view['kind'] in ('preview', 'confirmation'):
            self.enabled()
            state = calendar_ui.start(post['destination'], self.store.now(), self.config.timezone)
            local = datetime.fromtimestamp(post['due'], self.config.timezone)
            state.update(reprogram=True, month=local.strftime('%Y-%m'), day=local.date().isoformat(), hour=local.hour,
                         media=view['state'].get('media', False), _expires=self.store.now() + self.store.ttl)
            await self.send_scheduled_picker(post, state, view)
        elif action == 'edit' and view['kind'] == 'preview':
            if self.store.scheduled_editor(self.owner):
                raise ValueError('Ya hay una edición programada abierta. Termínala o ciérrala antes de abrir otra.')
            state = {'content': self.content(post), 'root': message_id,
                     'media': view['state'].get('media', bool(post['photo'] or post['photo_id'])), '_expires': self.store.now() + self.store.ttl}
            fresh = self.store.add_scheduled_view(self.owner, post['id'], post['revision'], 'editor', state, self.schedule_scope())
            text, keyboard = self.editor_controls(state['content'], f'q:{fresh}:')
            sent = await self.say(text, keyboard)
            self.store.bind_scheduled_view(self.owner, fresh, sent['message_id'])
            self.store.track_message(self.owner, message_id, state['_expires'])
            self.store.close_scheduled_view(self.owner, view_token)
            await self.strip_controls(message_id)
        elif action == 'save' and view['kind'] == 'review':
            previous = self.store.mutate_scheduled(self.owner, view_token, message_id, self.schedule_scope(), 'save')
            await self.invalidate_scheduled(previous)
            updated = self.store.get_post(post['id'])
            await self.open_scheduled(updated, message_id=message_id, previous_media=state.get('media', bool(post['photo'] or post['photo_id'])))
            self.store.close_scheduled_view(self.owner, view_token)
            await self.resolve_notice_events('saved', 'operation:q:save')
        else:
            raise Stale()

    async def scheduled_editor_action(self, query, view, post, action):
        state = view['state']
        if action in ('close', 'discard'):
            self.store.close_scheduled_view(self.owner, view['token'])
            await self.remove(view['message_id'])
            await self.open_scheduled(post, message_id=state['root'], previous_media=state['media'])
            await self.resolve_notice_events('editor-closed', abandon=True)
        elif action == 'back' and state.get('mode') in ('change', 'replace'):
            state.pop('mode', None)
            self.store.update_view_state(self.owner, view['token'], state)
            text, keyboard = self.editor_controls(state['content'], f"q:{view['token']}:")
            await self.replace_screen(view['message_id'], text, keyboard)
        elif action in ('change', 'replace'):
            if action == 'change' and not state['content'].get('template'):
                raise Stale()
            state['mode'] = action
            self.store.update_view_state(self.owner, view['token'], state)
            text = ('📷 Envía la nueva foto del producto. Se conservará la plantilla.' if action == 'change'
                    else '🖼️ Envía la imagen que quieres utilizar. No se aplicará la plantilla.')
            await self.replace_screen(view['message_id'], text, self.image_request_controls(f"q:{view['token']}:"))
        elif action == 'remove':
            content = dict(state['content'], photo=b'', photo_id='', template=False, source_photo=b'')
            await self.scheduled_review(view, post, content)
            await self.resolve_content_notices('image', content)
        elif action in ('prices', 'discount', 'keep') and state.get('pending'):
            pending = state['pending']
            content, _ = await self.text_input(pending['caption'], pending['entities'], state['content'], action)
            await self.scheduled_review(view, post, content)
            await self.resolve_content_notices('caption', content)
        else:
            raise Stale()
        await self.answer(query)

    async def receive_scheduled_edit(self, message, editor):
        if message['message_id'] <= editor['message_id']:
            raise Stale('Ese mensaje es anterior a la edición actual. Envíalo de nuevo.')
        post = self.store.scheduled_post(editor['post_id'], editor['revision'], self.owner, self.schedule_scope())
        state = editor['state']
        if message.get('photo'):
            content = await self.image_input(message, state['content'], state.get('mode'))
        else:
            caption, entities = message.get('text', ''), message.get('entities', [])
            content, conflict = await self.text_input(caption, entities, state['content'])
            if conflict:
                state['pending'] = {'caption': caption, 'entities': entities}
                self.store.update_view_state(self.owner, editor['token'], state)
                await self.replace_screen(editor['message_id'],
                    f'⚠️ Los precios indican un {conflict[0]} % de descuento,\npero has escrito un {conflict[1]} %. ¿Qué quieres conservar?',
                    self.conflict_controls(f"q:{editor['token']}:"))
                await self.remove(message['message_id'])
                return
        await self.scheduled_review(editor, post, content)
        await self.resolve_content_notices('image' if message.get('photo') else 'caption', content)
        await self.remove(message['message_id'])

    async def scheduled_review(self, editor, post, content):
        # Both-empty drafts can be kept for new offers, but not saved as an empty scheduled post.
        from .domain import validate_caption
        validate_caption(content['caption'], content['entities'], has_photo=bool(content['photo'] or content['photo_id']))
        state = {**content, 'media': bool(content['photo'] or content['photo_id']), '_expires': editor['state']['_expires']}
        fresh = self.store.add_scheduled_view(self.owner, post['id'], post['revision'], 'review', state, self.schedule_scope())
        keyboard = [[self.scheduled_button('✅ - Guardar', fresh, 'save'), self.scheduled_button('❌ - Cerrar', fresh, 'close')]]
        message_id = await self.write_content(content, keyboard, message_id=editor['state']['root'], previous_media=editor['state']['media'])
        self.store.bind_scheduled_view(self.owner, fresh, message_id)
        self.store.close_scheduled_view(self.owner, editor['token'])
        await self.remove(editor['message_id'])

    async def send_scheduled_picker(self, post, state, old=None):
        fresh = self.store.add_scheduled_view(self.owner, post['id'], post['revision'], 'picker', state, self.schedule_scope())
        text, entities, keyboard = self.picker_content(fresh, state)
        for row in keyboard:
            for button in row:
                button['callback_data'] = 'r:' + button['callback_data'][2:]
        if old:
            await self.replace_screen(old['message_id'], text, keyboard, entities, media=state.get('media', False))
            message_id = old['message_id']
        else:
            sent = await self.say(text, keyboard, entities)
            message_id = sent['message_id']
        self.store.bind_scheduled_view(self.owner, fresh, message_id)
        if old:
            self.store.close_scheduled_view(self.owner, old['token'])

    async def scheduled_picker_callback(self, query, view_token, action, value):
        message_id = query['message']['message_id']
        view = self.store.scheduled_view(self.owner, view_token, message_id)
        if view['kind'] != 'picker':
            raise Stale()
        post = self.store.scheduled_post(view['post_id'], view['revision'], self.owner, self.schedule_scope())
        if post['destination'] != self.config.channel:
            raise Stale('El destino ha cambiado. Revisa la configuración antes de modificar esta programación.')
        if action == 'close':
            await self.answer(query)
            await self.schedule_confirmation(post['id'], message_id=message_id, media=view['state'].get('media', False))
            self.store.close_scheduled_view(self.owner, view_token)
            await self.resolve_notice_events('schedule-abandoned')
            return
        if action == 'back' and view['state']['screen'] == 'days':
            await self.open_scheduled(post, message_id=message_id, previous_media=view['state'].get('media', False))
            self.store.close_scheduled_view(self.owner, view_token)
            await self.answer(query)
            await self.resolve_notice_events('schedule-abandoned')
            return
        self.enabled()
        calendar_ui.check_picker_zone(view['state'], self.config.timezone)
        await self.answer(query)
        if action == 'confirm':
            due = view['state'].get('due')
            previous = self.store.mutate_scheduled(self.owner, view_token, message_id, self.schedule_scope(), 'schedule', due=due)
            await self.invalidate_scheduled(previous)
            await self.schedule_confirmation(post['id'], message_id=message_id, media=view['state'].get('media', False))
            await self.resolve_notice_events('scheduled', 'operation:r:confirm')
        elif action != 'noop':
            try:
                state = calendar_ui.transition(view['state'], action, value, self.store.now(), self.config.timezone)
            except (KeyError, TypeError, OverflowError):
                raise ValueError('Selecciona de nuevo una fecha y hora válidas con los botones actuales.') from None
            await self.send_scheduled_picker(post, state, view)
