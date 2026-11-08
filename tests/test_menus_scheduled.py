"""Private UI and scheduled changes tested without real credentials or Telegram requests."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from canal_ofertas_bot import __version__, calendar_ui, ui
from canal_ofertas_bot.config import Config
from canal_ofertas_bot.domain import Product, compose_caption, units
from canal_ofertas_bot.service import Service, start_keyboard
from canal_ofertas_bot.storage import Store, Stale
from canal_ofertas_bot.post_ui import scheduled_card_text

OWNER, OTHER = 123456, 789012
URL = 'https://amzn.to/ficticio-0001'
CHANNEL = '-1001234567890'
CHANGED = '⚠️ Esta programación ha cambiado. Abre de nuevo Programadas para consultar su estado.'


class ScheduledTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp()
        self.store = Store(Path(self.tmp.name) / 'test.sqlite3', ttl=1800, now=lambda: self.now)
        self.config = Config.from_env({'TELEGRAM_BOT_TOKEN': '123456:' + 'a' * 35,
                                      'TELEGRAM_OWNER_ID': f'{OWNER},{OTHER}', 'TELEGRAM_CHANNEL_ID': CHANNEL,
                                      'DATA_DIR': self.tmp.name, 'PREVIEW_TTL_SECONDS': '1800'})
        self.serial = 100
        self.api = SimpleNamespace(message=AsyncMock(side_effect=self.sent), photo=AsyncMock(side_effect=self.sent),
                                   call=AsyncMock(return_value=True))
        self.bot = Service(self.config, self.store, self.api)
        self.product = Product(original_url=URL, title='Uno dos tres cuatro cinco seis siete',
                               price='179.99', reference='195.10', deals=['Oferta flash'])

    def sent(self, *args, **kwargs):
        self.serial += 1
        return {'message_id': self.serial, 'photo': [{'file_id': 'foto-ficticia'}]}

    def new_preview(self, owner=OWNER):
        self.store.replace_draft(owner, self.product, 1)
        caption, entities = compose_caption(self.product)
        preview = self.store.stage_preview(owner, CHANNEL, b'image', caption, entities)
        self.serial += 1
        self.store.activate(owner, preview, self.serial, 'foto-ficticia')
        return self.store.preview(owner)

    def new_post(self, owner=OWNER):
        preview = self.new_preview(owner)
        post = self.store.consume(owner, preview['id'], preview['message_id'], CHANNEL, due=self.now + 3600)
        return self.store.get_post(post)

    def view(self, post, kind='card', state=None, actor=OWNER):
        allowed = self.config.owners if self.config.shared_schedule else (actor,)
        token = self.store.add_scheduled_view(actor, post['id'], post['revision'], kind, state or {}, allowed)
        self.serial += 1
        self.store.bind_scheduled_view(actor, token, self.serial)
        return self.store.scheduled_view(actor, token)

    async def click(self, data, message_id, actor=OWNER, *, photo=False):
        await self.bot.handle({'callback_query': {'id': 'query', 'from': {'id': actor}, 'data': data,
                             'message': {'message_id': message_id, 'photo': [{'file_id': 'foto-ficticia'}] if photo else [],
                                         'chat': {'id': actor, 'type': 'private'}}}})

    async def q(self, view, action, actor=OWNER):
        await self.click(f"q:{view['token']}:{action}", view['message_id'], actor)

    def last_edit(self):
        for call in reversed(self.api.call.await_args_list):
            if call.args[0] in ("editMessageText", "editMessageCaption"):
                return call
            if call.args[0] == "editMessageMedia":
                return SimpleNamespace(args=call.args, kwargs={**call.kwargs,
                    "caption": call.kwargs["media"]["caption"],
                    "caption_entities": call.kwargs["media"].get("caption_entities", [])})
        raise AssertionError("No message edit was recorded")

    async def text(self, caption, actor=OWNER):
        self.serial += 1
        await self.bot.handle({'message': {'message_id': self.serial, 'text': caption,
                              'chat': {'id': actor, 'type': 'private'}, 'from': {'id': actor}}})

    def latest_view(self, kind, actor=OWNER):
        with self.store.connect() as db:
            row = db.execute('SELECT token FROM scheduled_views WHERE actor=? AND kind=? ORDER BY message_id DESC LIMIT 1',
                             (actor, kind)).fetchone()
        return self.store.scheduled_view(actor, row[0])

    def new_caption(self):
        return URL + '\n\n📦 Título editado con cinco palabras adicionales\n\n👉 De 250,00 € a 190,00 €'

    async def review(self, post):
        view = self.view(post, 'preview')
        await self.q(view, 'edit')
        await self.text(self.new_caption())
        return self.latest_view('review')

    async def test_home_format_and_real_daily_count(self):
        post = self.new_post()
        self.now += 3601
        self.assertIsNotNone(self.store.claim(OWNER, post['id']))
        self.store.finish(post['id'], 'sent', 1)
        self.new_post()
        await self.bot.home()
        text, keyboard, entities = self.api.message.call_args.args[1:4]
        self.assertIn('Publicadas hoy: 1 • Programadas: 1', text)
        bold = [text.encode('utf-16-le')[e['offset']*2:(e['offset']+e['length'])*2].decode('utf-16-le') for e in entities]
        self.assertEqual(bold, ['Canal Ofertas Bot', '1', '1'])
        self.assertEqual(keyboard, start_keyboard())
        self.now += 86400
        await self.bot.home()
        self.assertIn('Publicadas hoy: 0', self.api.message.call_args.args[1])

    async def test_home_respects_own_and_shared_scope(self):
        self.new_post(OTHER)
        await self.bot.home()
        self.assertIn('Programadas: 0', self.api.message.call_args.args[1])
        self.set_shared()
        await self.bot.home()
        self.assertIn('Programadas: 1', self.api.message.call_args.args[1])

    def set_shared(self):
        self.config = Config.from_env({**self.config.values, 'SCHEDULE_PERMISSIONS': 'all'})
        self.bot.config = self.config

    async def test_pending_empty_and_cards(self):
        await self.bot.show_pending()
        text, keyboard = self.api.message.call_args.args[1:3]
        self.assertIn('No hay publicaciones programadas pendientes.', text)
        self.assertEqual(text, '📅 No hay publicaciones programadas pendientes.')
        self.assertEqual(keyboard, ui.navigation())
        self.new_post()
        await self.bot.show_pending()
        text, _, keyboard = self.api.photo.call_args.args[2:5]
        self.assertIn('Uno dos tres cuatro cinco…', text)
        self.assertIn(f'Preparado por: {OWNER}', text)
        self.assertIn('(Madrid)', text)
        self.assertNotIn('Europe/Madrid', text)
        self.assertEqual([b['text'] for b in keyboard[0]], ['📝 - Borrador', '🚫 - Cancelar'])

    async def test_status_and_about_version(self):
        await self.bot.show_status()
        text, keyboard = self.api.message.call_args.args[1:3]
        self.assertIn('🔎 Extracción de Amazon: activada', text)
        self.assertIn(f'👥 Usuarios autorizados: {OWNER}, {OTHER}', text)
        self.assertNotIn('Bot en funcionamiento', text)
        self.assertEqual(keyboard, ui.navigation())
        await self.bot.menu_action('about')
        text, keyboard, _ = self.api.message.call_args.args[1:4]
        self.assertIn('Conoce el proyecto y encuentra más contenido de DanieloTech.', text)
        self.assertEqual([b['text'] for b in keyboard[-1]], ['⬅️ - Volver', '❌ - Cerrar'])
        self.assertEqual(keyboard[-2][0]['url'], 'https://x.com/danielotech')
        await self.bot.menu_action('version')
        text, keyboard, entities = self.api.message.call_args.args[1:4]
        self.assertIn(f'Versión: {__version__}', text)
        self.assertTrue(any(e['type'] == 'italic' for e in entities))
        self.assertEqual(keyboard, ui.navigation('about'))

    async def test_close_plain_message_deletes_it(self):
        await self.click('u:close', 101)
        self.api.call.assert_any_await('deleteMessage', chat_id=OWNER, message_id=101)

    async def test_open_scheduled_preserves_new_draft(self):
        post = self.new_post()
        preview = self.new_preview()
        draft = self.store.draft(OWNER)
        card = self.view(post)
        await self.q(card, 'open')
        self.assertEqual(self.store.draft(OWNER), draft)
        self.assertEqual(self.store.preview(OWNER), preview)
        edit = self.api.call.await_args
        self.assertEqual(edit.args[0], 'editMessageMedia')
        self.assertEqual(edit.kwargs['message_id'], card['message_id'])
        keyboard = edit.kwargs['reply_markup']['inline_keyboard']
        self.assertEqual([[b['text'] for b in row] for row in keyboard],
                         [['✅ - Publicar', '🕒 - Reprogramar'], ['✏️ - Editar', '🚫 - Cancelar'], ['❌ - Cerrar']])

    async def test_edit_waits_for_save_and_keeps_schedule(self):
        post = self.new_post()
        self.new_preview()
        draft = self.store.draft(OWNER)
        review = await self.review(post)
        self.assertEqual(self.store.get_post(post['id'])['caption'], post['caption'])
        await self.q(review, 'save')
        updated = self.store.get_post(post['id'])
        self.assertEqual(updated['caption'], self.new_caption())
        self.assertEqual(updated['due'], post['due'])
        self.assertEqual(updated['owner'], OWNER)
        self.assertEqual(updated['destination'], CHANNEL)
        self.assertEqual(len(updated['entities']), 2)
        self.assertEqual(self.store.draft(OWNER), draft)
        self.assertEqual(self.latest_view('preview')['post_id'], post['id'])
        self.assertFalse(any(c.args[1] == '✅ Cambios guardados.' for c in self.api.message.await_args_list))
        self.assertEqual(updated['revision'], post['revision'] + 1)

    async def test_close_review_does_not_save(self):
        post = self.new_post()
        review = await self.review(post)
        await self.q(review, 'close')
        self.assertEqual(self.store.get_post(post['id']), post)

    async def test_edited_url_is_allowed_and_waits_for_save(self):
        post = self.new_post()
        await self.q(self.view(post, 'preview'), 'edit')
        caption = self.new_caption().replace(URL, 'https://example.com/nuevo')
        await self.text(caption)
        review = self.latest_view('review')
        self.assertEqual(review['state']['caption'], caption)
        self.assertEqual(self.store.get_post(post['id']), post)
        await self.q(review, 'save')
        self.assertEqual(self.store.get_post(post['id'])['caption'], caption)

    async def test_discard_editor_keeps_programming(self):
        post = self.new_post()
        await self.q(self.view(post, 'preview'), 'edit')
        await self.q(self.latest_view('editor'), 'discard')
        self.assertIsNone(self.store.scheduled_editor(OWNER))
        self.assertEqual(self.store.get_post(post['id']), post)

    async def test_reprogram_full_flow_same_post_then_cancel(self):
        post = self.new_post()
        await self.q(self.view(post, 'confirmation', {'media': True}), 'schedule')
        picker = self.latest_view('picker')
        self.assertIn('reprogram', picker['state'])
        for action, value in [('day', '2026-11-06'), ('hour', '15'), ('minute', '50')]:
            await self.click(f"r:{picker['token']}:{action}:{value}", picker['message_id'])
            picker = self.latest_view('picker')
        self.assertEqual(self.store.get_post(post['id'])['due'], post['due'])
        await self.click(f"r:{picker['token']}:confirm:-", picker['message_id'])
        updated = self.store.get_post(post['id'])
        self.assertNotEqual(updated['due'], post['due'])
        self.assertEqual(len(self.store.pending_all((OWNER,))), 1)
        self.assertEqual(updated['caption'], post['caption'])
        self.assertIn('🕒 Pendiente\n06/11/2026 15:50 (Madrid)', self.last_edit().kwargs['caption'])
        confirmation = self.latest_view('confirmation')
        await self.q(confirmation, 'cancel')
        self.assertEqual(self.store.get_post(post['id'])['status'], 'cancelled')
        self.assertEqual(self.store.pending_all((OWNER,)), [])
        self.api.call.assert_any_await('editMessageCaption', chat_id=OWNER, message_id=confirmation['message_id'],
                                      caption=scheduled_card_text(self.store.get_post(post['id']), self.config.timezone, 'cancelled'), caption_entities=[], reply_markup={'inline_keyboard': []})

    async def test_close_reprogram_restores_confirmation_and_preserves_post(self):
        post = self.new_post()
        await self.q(self.view(post, 'confirmation', {'media': True}), 'schedule')
        picker = self.latest_view('picker')
        await self.click(f"r:{picker['token']}:close:-", picker['message_id'])
        self.assertEqual(self.store.get_post(post['id']), post)
        edit = self.last_edit()
        self.assertEqual(edit.kwargs['message_id'], picker['message_id'])
        self.assertTrue(edit.kwargs['caption'].startswith('🕒 Pendiente\n'))
        self.assertFalse(any(c.args[0] == 'deleteMessage' for c in self.api.call.await_args_list))

    async def test_publish_now_sends_once_and_removes_scheduled_send(self):
        post = self.new_post()
        view = self.view(post, 'preview')
        await self.q(view, 'publish')
        self.assertEqual(self.store.get_post(post['id'])['status'], 'sent')
        self.assertEqual(self.store.pending_all((OWNER,)), [])
        await self.q(view, 'publish')
        self.now += 3601
        await self.bot.delivery.tick()
        self.assertEqual(self.api.photo.await_count, 1)

    async def test_sent_or_sending_post_cannot_be_edited_or_reprogrammed(self):
        for finish in (False, True):
            post = self.new_post()
            view = self.view(post, 'preview')
            self.now = post['due'] + 1
            self.store.claim(OWNER, post['id'])
            if finish:
                self.store.finish(post['id'], 'sent', 1)
            await self.q(view, 'schedule')
            answer = self.api.call.call_args.kwargs['text']
            self.assertTrue(answer)

    async def test_due_post_sends_original_content_during_edit(self):
        post = self.new_post()
        review = await self.review(post)
        self.now = post['due'] + 1
        await self.bot.delivery.tick()
        self.assertIsNone(self.store.get_post(post['id']))
        self.assertEqual(self.api.photo.call_args.args[2], post['caption'])
        await self.q(review, 'save')
        self.assertTrue(self.api.call.call_args.kwargs['show_alert'])

    async def test_cross_user_changes_invalidate_old_buttons(self):
        self.set_shared()
        post = self.new_post()
        old = self.view(post, actor=OTHER)
        review = await self.review(post)
        await self.q(review, 'save')
        await self.q(old, 'cancel', OTHER)
        self.assertEqual(self.api.call.call_args.kwargs['text'], CHANGED)
        self.assertEqual(self.store.get_post(post['id'])['status'], 'pending')

    async def test_own_permissions_and_foreign_tokens_are_rejected(self):
        post = self.new_post()
        view = self.view(post)
        await self.q(view, 'cancel', OTHER)
        self.assertEqual(self.api.call.call_args.kwargs['text'], CHANGED)
        with self.assertRaises(Stale):
            self.store.scheduled_post(post['id'], post['revision'], OTHER, (OTHER,))
        self.assertEqual(self.store.get_post(post['id'])['status'], 'pending')

    async def test_wrong_message_id_and_double_cancel_are_rejected(self):
        post = self.new_post()
        view = self.view(post)
        await self.click(f"q:{view['token']}:cancel", view['message_id'] + 1)
        self.assertEqual(self.store.get_post(post['id'])['status'], 'pending')
        await self.q(view, 'cancel')
        await self.q(view, 'cancel')
        self.assertEqual(self.api.call.call_args.kwargs['text'], CHANGED)

    async def test_views_survive_restart(self):
        post = self.new_post()
        view = self.view(post)
        self.bot.store = Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now)
        await self.q(view, 'open')
        self.assertEqual(self.api.call.await_args.kwargs['media']['caption'], post['caption'])
        self.assertEqual(self.api.call.await_args.kwargs['message_id'], view['message_id'])

    async def test_shared_edit_keeps_original_author(self):
        self.set_shared()
        post = self.new_post(OTHER)
        review = await self.review(post)
        await self.q(review, 'save')
        self.assertEqual(self.store.get_post(post['id'])['owner'], OTHER)


    async def test_new_schedule_full_selector_flow(self):
        preview = self.new_preview()
        await self.click(f"p:{preview['id']}:schedule", preview['message_id'], photo=True)
        picker = self.store.current_picker(OWNER)
        for action, value in [('day', '2026-11-06'), ('hour', '09'), ('minute', '50')]:
            await self.click(f"s:{picker['token']}:{action}:{value}", picker['message_id'], photo=True)
            picker = self.store.current_picker(OWNER)
        await self.click(f"s:{picker['token']}:confirm:-", picker['message_id'], photo=True)
        posts = self.store.pending_all((OWNER,))
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['original_url'], URL)
        self.assertIn('🕒 Pendiente\n06/11/2026 09:50 (Madrid)', self.last_edit().kwargs['caption'])
        self.assertEqual([b['text'] for b in self.last_edit().kwargs['reply_markup']['inline_keyboard'][0]],
                         ['📝 - Borrador', '🚫 - Cancelar'])

    async def test_close_new_selector_preserves_preview_and_creates_no_post(self):
        preview = self.new_preview()
        await self.click(f"p:{preview['id']}:schedule", preview['message_id'])
        picker = self.store.current_picker(OWNER)
        await self.click(f"s:{picker['token']}:close:-", picker['message_id'])
        self.assertIsNone(self.store.current_picker(OWNER))
        self.assertEqual(self.store.draft(OWNER)['id'], preview['draft_id'])
        with self.assertRaises(Stale):
            self.store.preview(OWNER)
        self.assertEqual(self.store.pending_all((OWNER,)), [])
        self.api.call.assert_any_await('deleteMessage', chat_id=OWNER, message_id=picker['message_id'])

    async def test_normal_editor_survives_scheduled_edit(self):
        post = self.new_post()
        preview = self.new_preview()
        await self.click(f"p:{preview['id']}:edit", preview['message_id'])
        editor = self.store.editor(OWNER)
        await self.review(post)
        self.assertEqual(self.store.editor(OWNER), editor)
        self.assertEqual(self.store.preview(OWNER, editing=True)['id'], editor['preview_id'])

    async def test_reprogram_is_atomic_against_delivery_claim(self):
        from concurrent.futures import ThreadPoolExecutor
        post = self.new_post()
        new_due = post['due'] + 86400
        picker = self.view(post, 'picker', {'screen': 'review', 'due': new_due, 'destination': CHANNEL})
        self.now = post['due'] + 1
        def change():
            try:
                self.store.mutate_scheduled(OWNER, picker['token'], picker['message_id'], (OWNER,), 'schedule', due=new_due)
                return True
            except Stale:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            changed = pool.submit(change)
            claimed = pool.submit(self.store.claim, OWNER, post['id'])
            changed, claimed = changed.result(), claimed.result()
        self.assertNotEqual(changed, bool(claimed))
        current = self.store.get_post(post['id'])
        self.assertEqual(current['due'], new_due if changed else post['due'])
        self.assertEqual(current['status'], 'pending' if changed else 'sending')

    async def test_cancelled_post_cannot_be_saved_from_old_review(self):
        post = self.new_post()
        review = await self.review(post)
        card = self.view(post)
        await self.q(card, 'cancel')
        await self.q(review, 'save')
        self.assertEqual(self.api.call.call_args.kwargs['text'], CHANGED)
        self.assertEqual(self.store.get_post(post['id'])['status'], 'cancelled')

    async def test_double_save_does_not_overwrite_a_newer_revision(self):
        post = self.new_post()
        review = await self.review(post)
        await self.q(review, 'save')
        saved = self.store.get_post(post['id'])
        await self.q(review, 'save')
        self.assertEqual(self.store.get_post(post['id']), saved)
        self.assertEqual(self.api.call.call_args.kwargs['text'], CHANGED)

    async def test_manual_link_format_is_preserved_until_save(self):
        post = self.new_post()
        await self.q(self.view(post, 'preview'), 'edit')
        caption = self.new_caption()
        self.serial += 1
        entity = {'type': 'text_link', 'offset': 0, 'length': units(URL), 'url': 'https://example.com/otro'}
        await self.bot.handle({'message': {'message_id': self.serial, 'text': caption, 'entities': [entity],
                              'from': {'id': OWNER}, 'chat': {'id': OWNER, 'type': 'private'}}})
        self.assertIn(entity, self.latest_view('review')['state']['entities'])
        self.assertEqual(self.store.get_post(post['id']), post)

    async def test_help_has_separate_actions_and_close_button(self):
        await self.bot.menu_action('help')
        text, keyboard, entities = self.api.message.call_args.args[1:4]
        self.assertIn('🚫 Cancelar', text)
        self.assertIn('🚫 Cancelar', text)
        self.assertIn('❌ Cerrar', text)
        self.assertLessEqual(units(text), 4096)
        self.assertEqual(keyboard, ui.navigation())
        for entity in entities:
            text.encode('utf-16-le')[entity['offset']*2:(entity['offset']+entity['length'])*2].decode('utf-16-le')

    async def test_today_uses_configured_local_midnight(self):
        # Madrid: 23:30 UTC belongs to the following local calendar day.
        self.now = datetime(2026, 11, 5, 23, 30, tzinfo=timezone.utc).timestamp()
        with self.store.connect() as db:
            db.execute("INSERT INTO receipts VALUES ('local',?,'sent',?,1)", (OWNER, self.now))
            db.execute("INSERT INTO receipts VALUES ('previous',?,'sent',?,2)", (OWNER, self.now - 3600))
        await self.bot.home()
        self.assertIn('Publicadas hoy: 1', self.api.message.call_args.args[1])


    async def test_main_submenus_replace_message_without_brand_header(self):
        expected = {
            'prepare': '🛒 Para preparar la oferta, envíame un enlace de Amazon.es o de un acortador compatible.',
            'pending': '📅 No hay publicaciones programadas pendientes.',
            'users': '👥 Usuarios y privilegios:',
            'status': '📊 Estado y configuración del bot:',
            'help': '❓ Aprende a usar el bot:',
            'about': '👾 Conoce el proyecto y encuentra más contenido de DanieloTech.',
        }
        for action, first_line in expected.items():
            with self.subTest(action=action):
                await self.click('m:' + action, 500)
                edit = self.last_edit()
                self.assertEqual(edit.args[0], 'editMessageText')
                self.assertEqual(edit.kwargs['message_id'], 500)
                self.assertEqual(edit.kwargs['text'].splitlines()[0], first_line)
                self.assertNotIn('Canal Ofertas Bot • por DanieloTech', edit.kwargs['text'])
                keyboard = edit.kwargs['reply_markup']['inline_keyboard']
                self.assertEqual(keyboard[-1], ui.navigation()[0])
        self.api.message.assert_not_awaited()
        self.api.photo.assert_not_awaited()

    async def test_about_version_back_round_trip_in_one_message(self):
        for action in ('about', 'version', 'about', 'home'):
            await self.click('m:' + action, 501)
            edit = self.last_edit()
            self.assertEqual(edit.kwargs['message_id'], 501)
            text = edit.kwargs['text']
            if action == 'version':
                self.assertTrue(text.startswith('⚙️ Versión: '))
                self.assertNotIn('Hago vídeos', text)
                self.assertNotIn('Sígueme en redes', text)
                self.assertFalse(text.startswith('🛒 Canal Ofertas Bot'))
                self.assertEqual(edit.kwargs['reply_markup']['inline_keyboard'], ui.navigation('about'))
            elif action == 'home':
                self.assertTrue(text.startswith('🔥 Canal Ofertas Bot • por DanieloTech'))
                self.assertIn('Publicadas hoy: 0 • Programadas: 0', text)
        self.api.message.assert_not_awaited()

    async def test_calendar_back_restores_photo_caption_and_entities(self):
        preview = self.new_preview()
        await self.click(f"p:{preview['id']}:schedule", preview['message_id'], photo=True)
        picker = self.store.current_picker(OWNER)
        edit = self.last_edit()
        self.assertEqual(edit.args[0], 'editMessageCaption')
        self.assertEqual(edit.kwargs['message_id'], preview['message_id'])
        self.assertNotIn('Canal Ofertas Bot', edit.kwargs['caption'])
        self.assertEqual([b['text'] for b in edit.kwargs['reply_markup']['inline_keyboard'][-1]],
                         ['⬅️ - Volver', '❌ - Cerrar'])
        await self.click(f"s:{picker['token']}:back:-", picker['message_id'], photo=True)
        edit = self.last_edit()
        self.assertEqual(edit.kwargs['caption'], preview['caption'])
        self.assertEqual(edit.kwargs['caption_entities'], preview['entities'])
        self.assertIsNone(self.store.current_picker(OWNER))
        self.assertEqual(self.store.preview(OWNER), preview)
        self.api.message.assert_not_awaited()
        self.api.photo.assert_not_awaited()

    async def test_photo_schedule_steps_and_confirmation_keep_message_id(self):
        preview = self.new_preview()
        await self.click(f"p:{preview['id']}:schedule", preview['message_id'], photo=True)
        for action, value in [('day', '2026-11-06'), ('hour', '09'), ('all', '-'), ('minute', '51'), ('confirm', '-')]:
            picker = self.store.current_picker(OWNER)
            await self.click(f"s:{picker['token']}:{action}:{value}", picker['message_id'], photo=True)
            edit = self.last_edit()
            self.assertEqual(edit.args[0], 'editMessageCaption')
            self.assertEqual(edit.kwargs['message_id'], preview['message_id'])
        self.assertIn('🕒 Pendiente\n06/11/2026 09:51 (Madrid)', edit.kwargs['caption'])
        self.assertEqual(self.latest_view('confirmation')['message_id'], preview['message_id'])
        self.api.message.assert_not_awaited()
        self.api.photo.assert_not_awaited()

    async def test_cached_preview_controls_cannot_publish_during_picker(self):
        preview = self.new_preview()
        await self.click(f"p:{preview['id']}:schedule", preview['message_id'], photo=True)
        await self.click(f"p:{preview['id']}:publish", preview['message_id'], photo=True)
        self.assertIn('selector actual', self.api.call.call_args.kwargs['text'])
        self.assertEqual(self.store.pending_all((OWNER,)), [])
        self.api.photo.assert_not_awaited()

    async def test_reprogram_from_photo_stays_in_photo_and_can_restore_it(self):
        post = self.new_post()
        await self.bot.open_scheduled(post)
        view = self.latest_view('preview')
        photos_before = self.api.photo.await_count
        await self.q(view, 'schedule')
        picker = self.latest_view('picker')
        self.assertEqual(picker['message_id'], view['message_id'])
        self.assertEqual(self.last_edit().args[0], 'editMessageCaption')
        await self.click(f"r:{picker['token']}:back:-", picker['message_id'])
        edit = self.api.call.await_args_list[-2]
        self.assertEqual(edit.args[0], 'editMessageMedia')
        self.assertEqual(edit.kwargs['message_id'], view['message_id'])
        self.assertEqual(edit.kwargs['media']['caption'], post['caption'])
        self.assertEqual(edit.kwargs['media']['caption_entities'], post['entities'])
        self.assertEqual(self.latest_view('preview')['message_id'], view['message_id'])
        self.assertEqual(self.api.photo.await_count, photos_before)
        self.assertEqual(self.store.get_post(post['id']), post)
        self.api.message.assert_not_awaited()

    async def test_reprogram_from_text_confirmation_restores_photo_in_same_message(self):
        post = self.new_post()
        view = self.view(post, 'confirmation')
        await self.q(view, 'schedule')
        picker = self.latest_view('picker')
        self.assertEqual(self.last_edit().args[0], 'editMessageText')
        await self.click(f"r:{picker['token']}:back:-", picker['message_id'])
        restored = self.latest_view('preview')
        self.assertEqual(restored['message_id'], view['message_id'])
        self.assertTrue(restored['state']['media'])
        await self.q(restored, 'schedule')
        self.assertEqual(self.last_edit().args[0], 'editMessageCaption')
        self.api.message.assert_not_awaited()
        self.api.photo.assert_not_awaited()

    async def test_reprogram_photo_confirm_then_cancel_uses_caption_api(self):
        post = self.new_post()
        view = self.view(post, 'preview', {'media': True})
        await self.q(view, 'schedule')
        for action, value in [('day', '2026-11-06'), ('hour', '15'), ('minute', '50'), ('confirm', '-')]:
            picker = self.latest_view('picker')
            await self.click(f"r:{picker['token']}:{action}:{value}", picker['message_id'])
        confirmation = self.latest_view('confirmation')
        self.assertEqual(confirmation['message_id'], view['message_id'])
        self.assertIn('🕒 Pendiente', self.last_edit().kwargs['caption'])
        await self.q(confirmation, 'cancel')
        self.assertEqual(self.last_edit().kwargs['caption'], scheduled_card_text(self.store.get_post(post['id']), self.config.timezone, 'cancelled'))
        self.assertEqual(self.last_edit().args[0], 'editMessageCaption')
        self.assertEqual(self.store.get_post(post['id'])['status'], 'cancelled')
        self.api.message.assert_not_awaited()

    async def test_all_programming_back_buttons_share_row_with_close(self):
        for reprogram in (False, True):
            state = calendar_ui.start(CHANNEL, self.now)
            state.update(reprogram=reprogram, day='2026-11-06', hour=9, due=self.now + 86400)
            for screen in ('days', 'hours', 'minutes', 'all_minutes', 'review'):
                state['screen'] = screen
                text, entities, keyboard = self.bot.picker_content('t' * 16, state)
                self.assertNotIn('Canal Ofertas Bot', text)
                self.assertEqual(entities, [])
                self.assertLessEqual(units(text), 1024)
                self.assertEqual([b['text'] for b in keyboard[-1]], ['⬅️ - Volver', '❌ - Cerrar'])


    async def test_schedule_and_reschedule_confirmations_offer_draft_and_cancel(self):
        post = self.new_post()
        for reprogram in (False, True):
            await self.bot.schedule_confirmation(post['id'],
                                                 message_id=700, media=True)
            buttons = self.last_edit().kwargs['reply_markup']['inline_keyboard'][0]
            self.assertEqual([b['text'] for b in buttons], ['📝 - Borrador', '🚫 - Cancelar'])
            self.assertTrue(buttons[0]['callback_data'].endswith(':open'))
            self.assertTrue(buttons[1]['callback_data'].endswith(':cancel'))

    async def test_confirmation_draft_opens_complete_preview_in_same_message(self):
        post = self.new_post()
        for media in (True, False):
            confirmation = self.view(post, 'confirmation', {'media': media})
            await self.q(confirmation, 'open')
            edit = self.api.call.await_args
            self.assertEqual(edit.args[0], 'editMessageMedia')
            self.assertEqual(edit.kwargs['message_id'], confirmation['message_id'])
            self.assertEqual(edit.kwargs['media']['caption'], post['caption'])
            self.assertEqual(edit.kwargs['media']['caption_entities'], post['entities'])
            self.assertEqual(edit.kwargs['media']['media'], post['photo_id'])
            buttons = edit.kwargs['reply_markup']['inline_keyboard']
            self.assertEqual([[b['text'] for b in row] for row in buttons],
                             [['✅ - Publicar', '🕒 - Reprogramar'], ['✏️ - Editar', '🚫 - Cancelar'], ['❌ - Cerrar']])
            self.assertEqual(self.store.get_post(post['id']), post)
            self.assertEqual(self.latest_view('preview')['message_id'], confirmation['message_id'])
        self.api.photo.assert_not_awaited()
        self.api.message.assert_not_awaited()

    async def test_closing_confirmation_draft_preserves_schedule_and_delivery(self):
        post = self.new_post()
        with self.store.connect() as db:
            original_id = db.execute('SELECT message_id FROM publication_cards WHERE post_id=?', (post['id'],)).fetchone()[0]
        await self.bot.schedule_confirmation(post['id'], message_id=original_id, media=True)
        await self.q(self.latest_view('confirmation'), 'open')
        await self.q(self.latest_view('preview'), 'close')
        self.assertEqual(self.store.get_post(post['id']), post)
        self.assertEqual(self.store.pending_all((OWNER,)), [post])
        self.assertEqual(self.last_edit().kwargs['message_id'], original_id)
        self.assertTrue(self.last_edit().kwargs['caption'].startswith('🕒 Pendiente\n'))
        self.assertFalse(any(c.args[0] == 'deleteMessage' for c in self.api.call.await_args_list))
        self.now = post['due'] + 1
        await self.bot.delivery.tick()
        self.api.photo.assert_awaited_once_with(CHANNEL, post['photo_id'], post['caption'], post['entities'])
        self.api.call.assert_any_await('editMessageCaption', chat_id=OWNER, message_id=original_id,
                                      caption=scheduled_card_text(post, self.config.timezone, 'sent'),
                                      caption_entities=[], reply_markup={'inline_keyboard': []})
        self.api.message.assert_not_awaited()

    async def test_old_confirmation_cancel_is_inert_after_opening_draft(self):
        post = self.new_post()
        confirmation = self.view(post, 'confirmation', {'media': True})
        await self.q(confirmation, 'open')
        await self.q(confirmation, 'cancel')
        self.assertEqual(self.store.get_post(post['id']), post)
        self.assertIn('Esta programación ha cambiado', self.api.call.await_args.kwargs['text'])

    async def test_confirmation_draft_can_publish_now_without_duplicate(self):
        post = self.new_post()
        await self.q(self.view(post, 'confirmation', {'media': True}), 'open')
        await self.q(self.latest_view('preview'), 'publish')
        self.assertEqual(self.store.get_post(post['id'])['status'], 'sent')
        self.assertEqual(self.store.pending_all((OWNER,)), [])
        self.now = post['due'] + 1
        await self.bot.delivery.tick()
        self.api.photo.assert_awaited_once_with(CHANNEL, post['photo_id'], post['caption'], post['entities'])

    async def test_confirmation_draft_reprograms_then_offers_draft_again(self):
        post = self.new_post()
        await self.q(self.view(post, 'confirmation', {'media': True}), 'open')
        await self.q(self.latest_view('preview'), 'schedule')
        for action, value in [('day', '2026-11-06'), ('hour', '15'), ('minute', '50'), ('confirm', '-')]:
            picker = self.latest_view('picker')
            await self.click(f"r:{picker['token']}:{action}:{value}", picker['message_id'], photo=True)
        updated = self.store.get_post(post['id'])
        self.assertNotEqual(updated['due'], post['due'])
        self.assertEqual(updated['caption'], post['caption'])
        self.assertEqual(len(self.store.pending_all((OWNER,))), 1)
        self.assertTrue(self.last_edit().kwargs['caption'].startswith('🕒 Pendiente\n'))
        buttons = self.last_edit().kwargs['reply_markup']['inline_keyboard'][0]
        self.assertEqual([b['text'] for b in buttons], ['📝 - Borrador', '🚫 - Cancelar'])
        await self.q(self.latest_view('confirmation'), 'open')
        await self.q(self.latest_view('preview'), 'edit')
        self.assertIsNotNone(self.store.scheduled_editor(OWNER))
        self.assertEqual(self.store.get_post(post['id']), updated)



    async def test_close_each_reprogram_step_restores_exact_confirmation_in_same_photo(self):
        paths = {
            'days': [],
            'hours': [('day', '2026-11-06')],
            'minutes': [('day', '2026-11-06'), ('hour', '15')],
            'all_minutes': [('day', '2026-11-06'), ('hour', '15'), ('all', '-')],
            'review': [('day', '2026-11-06'), ('hour', '15'), ('minute', '50')],
        }
        for reprogram in (False, True):
            for screen, path in paths.items():
                with self.subTest(reprogram=reprogram, screen=screen):
                    post = self.new_post()
                    await self.bot.schedule_confirmation(post['id'],
                                                         message_id=self.serial, media=True)
                    original = self.last_edit().kwargs['caption']
                    original_id = self.latest_view('confirmation')['message_id']
                    await self.q(self.latest_view('confirmation'), 'open')
                    await self.q(self.latest_view('preview'), 'schedule')
                    for action, value in path:
                        picker = self.latest_view('picker')
                        await self.click(f"r:{picker['token']}:{action}:{value}", picker['message_id'], photo=True)
                    picker = self.latest_view('picker')
                    self.assertEqual(picker['state']['screen'], screen)
                    self.api.call.reset_mock()
                    await self.click(f"r:{picker['token']}:close:-", picker['message_id'], photo=True)
                    edit = self.last_edit()
                    self.assertEqual(edit.args[0], 'editMessageCaption')
                    self.assertEqual(edit.kwargs['caption'], original)
                    self.assertEqual(edit.kwargs['message_id'], original_id)
                    self.assertEqual([b['text'] for b in edit.kwargs['reply_markup']['inline_keyboard'][0]],
                                     ['📝 - Borrador', '🚫 - Cancelar'])
                    self.assertEqual(self.store.get_post(post['id']), post)
                    self.assertFalse(any(c.args[0] == 'deleteMessage' for c in self.api.call.await_args_list))
                    with self.assertRaises(Stale):
                        self.store.scheduled_view(OWNER, picker['token'])
        self.api.photo.assert_not_awaited()
        self.api.message.assert_not_awaited()

    async def test_close_reprogram_after_back_and_restart_keeps_reprogrammed_label(self):
        post = self.new_post()
        await self.bot.schedule_confirmation(post['id'],
                                             message_id=self.serial, media=True)
        original = self.last_edit().kwargs['caption']
        await self.q(self.latest_view('confirmation'), 'open')
        await self.q(self.latest_view('preview'), 'schedule')
        picker = self.latest_view('picker')
        await self.click(f"r:{picker['token']}:back:-", picker['message_id'], photo=True)
        await self.q(self.latest_view('preview'), 'schedule')
        picker = self.latest_view('picker')
        await self.click(f"r:{picker['token']}:day:2026-11-06", picker['message_id'], photo=True)
        picker = self.latest_view('picker')
        # The selector state is persistent, not an in-memory navigation history.
        restarted = Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now)
        self.bot.store = restarted
        self.bot.delivery.store = restarted
        await self.click(f"r:{picker['token']}:close:-", picker['message_id'], photo=True)
        self.assertEqual(self.last_edit().kwargs['caption'], original)
        self.assertTrue(original.startswith('🕒 Pendiente\n'))
        await self.q(self.latest_view('confirmation'), 'cancel')
        self.assertEqual(self.store.get_post(post['id'])['status'], 'cancelled')
        self.assertEqual(self.last_edit().kwargs['caption'], scheduled_card_text(self.store.get_post(post['id']), self.config.timezone, 'cancelled'))

    async def test_initial_scheduling_close_still_deletes_at_every_step(self):
        paths = [[], [('day', '2026-11-06')], [('day', '2026-11-06'), ('hour', '15')],
                 [('day', '2026-11-06'), ('hour', '15'), ('all', '-')],
                 [('day', '2026-11-06'), ('hour', '15'), ('minute', '50')]]
        for path in paths:
            with self.subTest(path=path):
                preview = self.new_preview()
                await self.click(f"p:{preview['id']}:schedule", preview['message_id'], photo=True)
                for action, value in path:
                    picker = self.store.current_picker(OWNER)
                    await self.click(f"s:{picker['token']}:{action}:{value}", picker['message_id'], photo=True)
                picker = self.store.current_picker(OWNER)
                self.api.call.reset_mock()
                await self.click(f"s:{picker['token']}:close:-", picker['message_id'], photo=True)
                self.api.call.assert_any_await('deleteMessage', chat_id=OWNER, message_id=preview['message_id'])
                self.assertIsNone(self.store.current_picker(OWNER))
                self.assertEqual(self.store.pending_all((OWNER,)), [])
                self.assertFalse(any(c.args[0] == 'editMessageCaption' for c in self.api.call.await_args_list))
        self.api.message.assert_not_awaited()

    async def test_closing_stale_reprogram_does_not_restore_cancelled_schedule(self):
        post = self.new_post()
        await self.q(self.view(post, 'preview', {'media': True}), 'schedule')
        picker = self.latest_view('picker')
        self.assertTrue(self.store.cancel_post(OWNER, post['id']))
        self.api.call.reset_mock()
        await self.click(f"r:{picker['token']}:close:-", picker['message_id'], photo=True)
        self.assertIn('Esta programación ha cambiado', self.api.call.await_args.kwargs['text'])
        self.assertEqual(self.store.get_post(post['id'])['status'], 'cancelled')
        self.assertFalse(any(c.args[0] == 'editMessageCaption' for c in self.api.call.await_args_list))

    async def test_closing_after_delivery_does_not_restore_old_schedule(self):
        post = self.new_post()
        await self.q(self.view(post, 'preview', {'media': True}), 'schedule')
        picker = self.latest_view('picker')
        self.now = post['due'] + 1
        self.store.claim(OWNER, post['id'])
        self.store.finish(post['id'], 'sent', 999)
        self.api.call.reset_mock()
        await self.click(f"r:{picker['token']}:close:-", picker['message_id'], photo=True)
        self.assertTrue(self.api.call.await_args.kwargs['show_alert'])
        self.assertEqual(self.store.get_post(post['id'])['status'], 'sent')
        self.assertFalse(any(c.args[0] == 'editMessageCaption' for c in self.api.call.await_args_list))



    async def test_draft_close_restores_programmed_or_reprogrammed_confirmation_repeatedly(self):
        for reprogram in (False, True):
            with self.subTest(reprogram=reprogram):
                post = self.new_post()
                await self.bot.schedule_confirmation(post['id'],
                                                     message_id=self.serial, media=True)
                original = self.last_edit().kwargs['caption']
                message_id = self.latest_view('confirmation')['message_id']
                for _ in range(3):
                    await self.q(self.latest_view('confirmation'), 'open')
                    preview = self.latest_view('preview')
                    await self.q(preview, 'close')
                    edit = self.last_edit()
                    self.assertEqual(edit.args[0], 'editMessageCaption')
                    self.assertEqual(edit.kwargs['caption'], original)
                    self.assertEqual(edit.kwargs['message_id'], message_id)
                    self.assertEqual([b['text'] for b in edit.kwargs['reply_markup']['inline_keyboard'][0]],
                                     ['📝 - Borrador', '🚫 - Cancelar'])
                    self.assertEqual(self.store.get_post(post['id']), post)
                    with self.assertRaises(Stale):
                        self.store.scheduled_view(OWNER, preview['token'])
                await self.q(self.latest_view('confirmation'), 'cancel')
                self.assertEqual(self.store.get_post(post['id'])['status'], 'cancelled')
        self.assertFalse(any(c.args[0] == 'deleteMessage' for c in self.api.call.await_args_list))
        self.api.message.assert_not_awaited()
        self.api.photo.assert_not_awaited()

    async def test_close_draft_after_calendar_back_recovers_original_confirmation(self):
        post = self.new_post()
        await self.bot.schedule_confirmation(post['id'],
                                             message_id=self.serial, media=True)
        original = self.last_edit().kwargs['caption']
        await self.q(self.latest_view('confirmation'), 'open')
        await self.q(self.latest_view('preview'), 'schedule')
        picker = self.latest_view('picker')
        await self.click(f"r:{picker['token']}:back:-", picker['message_id'], photo=True)
        await self.q(self.latest_view('preview'), 'close')
        self.assertEqual(self.last_edit().kwargs['caption'], original)
        self.assertEqual(self.store.get_post(post['id']), post)
        self.api.message.assert_not_awaited()

    async def test_close_programmed_draft_preserves_independent_new_offer(self):
        post = self.new_post()
        await self.bot.open_scheduled(post)
        programmed_preview = self.latest_view('preview')
        new_offer = self.new_preview()
        draft = self.store.draft(OWNER)
        await self.q(programmed_preview, 'close')
        self.assertEqual(self.store.preview(OWNER), new_offer)
        self.assertEqual(self.store.draft(OWNER), draft)
        self.assertEqual(self.store.get_post(post['id']), post)
        self.assertEqual(self.last_edit().kwargs['message_id'], programmed_preview['message_id'])
        self.assertTrue(self.last_edit().kwargs['caption'].startswith('🕒 Pendiente\n'))

    async def test_close_stale_draft_does_not_restore_cancelled_or_sent_post(self):
        for status in ('cancelled', 'sent'):
            with self.subTest(status=status):
                post = self.new_post()
                view = self.view(post, 'preview', {'media': True})
                if status == 'cancelled':
                    self.store.cancel_post(OWNER, post['id'])
                else:
                    self.now = post['due'] + 1
                    self.store.claim(OWNER, post['id'])
                    self.store.finish(post['id'], 'sent', 99)
                self.api.call.reset_mock()
                await self.q(view, 'close')
                self.assertEqual(self.store.get_post(post['id'])['status'], status)
                self.assertFalse(any(c.args[0] in ('editMessageCaption', 'deleteMessage')
                                     for c in self.api.call.await_args_list))
                self.assertTrue(self.api.call.await_args.kwargs['show_alert'])



    async def test_pending_cards_use_stored_photos_in_due_order(self):
        first = self.new_post()
        second = self.new_post()
        with self.store.connect() as db:
            db.execute('UPDATE posts SET due=?,scheduled_due=?,photo_id=? WHERE id=?', (self.now + 7200, self.now + 7200, 'photo-later', first['id']))
            db.execute('UPDATE posts SET photo_id=NULL WHERE id=?', (second['id'],))
        await self.bot.show_pending()
        photos = self.api.photo.await_args_list
        self.assertEqual(len(photos), 2)
        self.assertEqual(photos[0].args[:2], (OWNER, second['photo']))
        self.assertEqual(photos[1].args[:2], (OWNER, 'photo-later'))
        self.assertTrue(photos[0].args[2].startswith('🕒 Pendiente\n' + calendar_ui.when_short(second['due'], self.config.timezone)))
        self.assertTrue(photos[1].args[2].startswith('🕒 Pendiente\n' + calendar_ui.when_short(self.now + 7200, self.config.timezone)))
        for sent in photos:
            self.assertEqual(len(sent.args[2].splitlines()), 5)
            self.assertIn('Uno dos tres cuatro cinco…', sent.args[2])
            self.assertEqual(sent.args[3], [])
            self.assertEqual([b['text'] for b in sent.args[4][0]], ['📝 - Borrador', '🚫 - Cancelar'])
        self.api.message.assert_not_awaited()

    async def test_pending_photo_opens_and_closes_draft_in_same_card(self):
        post = self.new_post()
        await self.bot.show_pending()
        card = self.latest_view('card')
        photos_before = self.api.photo.await_count
        self.assertTrue(card['state']['media'])
        await self.q(card, 'open')
        edit = self.api.call.await_args
        self.assertEqual(edit.args[0], 'editMessageMedia')
        self.assertEqual(edit.kwargs['message_id'], card['message_id'])
        self.assertEqual(edit.kwargs['media']['caption'], post['caption'])
        self.assertEqual(edit.kwargs['media']['caption_entities'], post['entities'])
        await self.q(self.latest_view('preview'), 'close')
        restored = self.last_edit()
        self.assertEqual(restored.kwargs['message_id'], card['message_id'])
        self.assertEqual(restored.kwargs['caption'], self.bot.current_confirmation_text(post))
        self.assertEqual(self.store.get_post(post['id']), post)
        self.assertEqual(self.api.photo.await_count, photos_before)
        self.api.message.assert_not_awaited()

    async def test_cancelling_pending_photo_changes_caption_only_for_that_post(self):
        first = self.new_post()
        second = self.new_post()
        await self.bot.show_pending()
        card = self.latest_view('card')
        await self.q(card, 'cancel')
        cancelled = self.store.get_post(card['post_id'])
        self.assertEqual(cancelled['status'], 'cancelled')
        other_id = second['id'] if card['post_id'] == first['id'] else first['id']
        self.assertEqual(self.store.get_post(other_id)['status'], 'pending')
        edit = self.last_edit()
        self.assertEqual(edit.args[0], 'editMessageCaption')
        self.assertEqual(edit.kwargs['caption'], scheduled_card_text(cancelled, self.config.timezone, 'cancelled'))
        self.assertEqual(edit.kwargs['message_id'], card['message_id'])
        self.api.message.assert_not_awaited()

    async def test_reprogrammed_pending_card_remembers_label_after_restart_and_view_cleanup(self):
        post = self.new_post()
        due = self.now + 7200
        picker = self.view(post, 'picker', {'screen': 'review', 'due': due, 'destination': CHANNEL, 'media': True})
        self.store.mutate_scheduled(OWNER, picker['token'], picker['message_id'], (OWNER,), 'schedule', due=due)
        with self.store.connect() as db:
            db.execute('DELETE FROM scheduled_views WHERE post_id=?', (post['id'],))
        restarted = Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now)
        self.store = restarted
        self.bot.store = restarted
        self.bot.delivery.store = restarted
        await self.bot.show_pending()
        card = self.latest_view('card')
        await self.q(card, 'open')
        await self.q(self.latest_view('preview'), 'schedule')
        picker = self.latest_view('picker')
        await self.click(f"r:{picker['token']}:close:-", picker['message_id'], photo=True)
        text = self.last_edit().kwargs['caption']
        self.assertEqual(text, scheduled_card_text(self.store.get_post(post['id']), self.config.timezone))
        self.assertEqual(self.last_edit().kwargs['message_id'], card['message_id'])
        self.assertEqual(self.store.get_post(post['id'])['due'], due)
        await self.q(self.latest_view('confirmation'), 'open')
        await self.q(self.latest_view('preview'), 'close')
        self.assertEqual(self.last_edit().kwargs['caption'], text)

    async def test_existing_reprogrammed_confirmation_supplies_pending_card_label(self):
        post = self.new_post()
        await self.bot.schedule_confirmation(post['id'], message_id=self.serial, media=True)
        with self.store.connect() as db:
            db.execute("DELETE FROM meta WHERE key LIKE 'reprogrammed:%'")
        await self.bot.show_pending()
        card = self.latest_view('card')
        await self.q(card, 'open')
        await self.q(self.latest_view('preview'), 'close')
        self.assertTrue(self.last_edit().kwargs['caption'].startswith('🕒 Pendiente\n'))
        self.assertEqual(self.last_edit().kwargs['message_id'], card['message_id'])

    async def test_programmed_pending_photos_respect_own_and_shared_permissions(self):
        self.new_post(OTHER)
        await self.bot.show_pending()
        self.api.photo.assert_not_awaited()
        self.set_shared()
        await self.bot.show_pending()
        self.assertEqual(self.api.photo.await_count, 1)
        self.assertIn(f'Preparado por: {OTHER}', self.api.photo.await_args.args[2])
        card = self.latest_view('card')
        await self.q(card, 'open')
        self.assertEqual(self.api.call.await_args.kwargs['message_id'], card['message_id'])



class CalendarTests(unittest.TestCase):
    def test_city_only_and_reprogram_questions(self):
        now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp()
        zone = ZoneInfo('America/New_York')
        state = calendar_ui.start(CHANNEL, now, zone)
        state['reprogram'] = True
        for screen in ('days', 'hours', 'minutes', 'review'):
            state.update(screen=screen, day='2026-11-06', hour=15, due=now + 86400)
            text, keyboard = calendar_ui.view('t' * 16, state, now, zone)
            self.assertNotIn('America/', text)
            self.assertNotIn('Todavía no', text)
            self.assertEqual([b['text'] for b in keyboard[-1]], ['⬅️ - Volver', '❌ - Cerrar'])
            if screen == 'review':
                self.assertTrue(text.startswith('¿Reprogramar para '))
                self.assertEqual(len(text.splitlines()), 2)
                self.assertEqual(keyboard[0][0]['text'], '✅ - Confirmar')
            else:
                self.assertIn('Horario de New York.', text)

    def test_dst_and_past_dates_still_rejected(self):
        now = datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()
        with self.assertRaisesRegex(ValueError, 'no existe'):
            calendar_ui.future_instant(datetime(2026, 3, 29, 2, 30), now)
        with self.assertRaisesRegex(ValueError, 'futuras'):
            calendar_ui.future_instant(datetime(2026, 2, 28, 12), now)

    def test_all_callback_data_within_telegram_limit(self):
        now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp()
        state = calendar_ui.start(CHANNEL, now)
        _, keyboard = calendar_ui.view('t' * 16, state, now)
        for row in keyboard:
            for button in row:
                self.assertLessEqual(len(button['callback_data'].encode()), 64)


class MigrationTests(unittest.TestCase):
    def test_v2_pending_post_migrates_without_losing_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db.sqlite3'
            store = Store(path)
            with store.connect() as db:
                db.execute("INSERT INTO posts(id,owner,preview_id,destination,caption,entities,photo,title,due,created,scheduled,status) "
                           "VALUES ('old',1,'preview',?,?,'[]',x'01','Producto',9999999999,1,1,'pending')", (CHANNEL, URL + '\nProducto'))
                db.execute('ALTER TABLE posts DROP COLUMN original_url')
                db.execute('ALTER TABLE posts DROP COLUMN revision')
                db.execute('PRAGMA user_version=2')
            upgraded = Store(path)
            post = upgraded.get_post('old')
            self.assertEqual(post['original_url'], URL)
            self.assertEqual(post['revision'], 1)
            self.assertEqual(post['status'], 'pending')
            self.assertEqual(post['photo'], b'\x01')
            with upgraded.connect() as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 6)
