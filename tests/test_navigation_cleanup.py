"""Private navigation and cleanup; all Telegram/Amazon calls are simulated."""
import ast
import hashlib
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
import test_menus_scheduled as fixtures
from test_menus_scheduled import OWNER, OTHER, CHANNEL, URL
from canal_ofertas_bot.amazon import URL_WARNING, validate_url
from canal_ofertas_bot.domain import Product
from canal_ofertas_bot.service import Service, HELP
from canal_ofertas_bot.storage import Store, Stale
from canal_ofertas_bot import ui


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ScheduledTests.setUp
    sent = fixtures.ScheduledTests.sent
    new_preview = fixtures.ScheduledTests.new_preview
    new_post = fixtures.ScheduledTests.new_post
    view = fixtures.ScheduledTests.view
    click = fixtures.ScheduledTests.click
    q = fixtures.ScheduledTests.q
    text = fixtures.ScheduledTests.text
    latest_view = fixtures.ScheduledTests.latest_view

    def reset_api(self):
        self.api.message.reset_mock()
        self.api.photo.reset_mock()
        self.api.call.reset_mock()

    def deleted(self, actor=OWNER):
        return {c.kwargs['message_id'] for c in self.api.call.await_args_list
                if c.args[0] == 'deleteMessage' and c.kwargs['chat_id'] == actor}

    def edits(self):
        return [c for c in self.api.call.await_args_list
                if c.args[0] in ('editMessageText', 'editMessageCaption', 'editMessageMedia')]

    def assert_menu_untouched(self, message_id):
        mutations = {'deleteMessage', 'editMessageText', 'editMessageCaption', 'editMessageMedia', 'editMessageReplyMarkup'}
        self.assertFalse(any(c.args[0] in mutations and c.kwargs.get('message_id') == message_id
                             for c in self.api.call.await_args_list))

    async def prepare(self, message_id=500):
        await self.click('m:prepare', message_id)
        self.assertEqual(self.store.preparation(OWNER)['message_id'], message_id)

    async def test_draft_is_independent_and_menu_is_preserved(self):
        before = self.new_preview()
        self.reset_api()
        await self.click('m:draft', 500)
        current = self.store.preview(OWNER)
        self.assertNotEqual(current['message_id'], 500)
        self.assertEqual(current['caption'], before['caption'])
        self.assertEqual(current['expires'], before['expires'])
        self.api.photo.assert_awaited_once()
        self.api.message.assert_not_awaited()
        self.assertIn(before['message_id'], self.deleted())
        self.assert_menu_untouched(500)
        sent = self.api.photo.await_args
        self.assertTrue(sent.args[2].startswith('📝 Borrador'))
        self.assertEqual([[b['text'] for b in row] for row in sent.args[4]],
                         [['👀 - Abrir', '🚫 - Cancelar'], ['❌ - Cerrar']])

    async def test_draft_open_shows_original_content_and_controls(self):
        before = self.new_preview()
        await self.click('m:draft', 500)
        current = self.store.preview(OWNER)
        await self.click(f"d:{current['id']}:open", current['message_id'], photo=True)
        update = self.edits()[-1]
        self.assertEqual(update.kwargs['message_id'], current['message_id'])
        self.assertEqual(update.kwargs['media']['caption'], before['caption'])
        self.assertEqual([[b['text'] for b in row] for row in update.kwargs['reply_markup']['inline_keyboard']],
                         [['✅ - Publicar', '🕒 - Programar'], ['✏️ - Editar', '🚫 - Cancelar'], ['❌ - Cerrar']])
        self.assert_menu_untouched(500)

    async def test_close_photo_card_keeps_draft_without_resending_menu(self):
        before = self.new_preview()
        await self.click('m:draft', 500)
        current = self.store.preview(OWNER)
        self.reset_api()
        await self.click(f"d:{current['id']}:close", current['message_id'], photo=True)
        self.assertEqual(self.store.saved_preview(OWNER)['caption'], before['caption'])
        self.assertIsNone(self.store.saved_preview(OWNER)['message_id'])
        self.api.message.assert_not_awaited()
        self.api.photo.assert_not_awaited()
        self.assertIn(current['message_id'], self.deleted())
        self.assert_menu_untouched(500)
        self.assertIsNotNone(self.store.draft(OWNER))

    async def test_close_text_card_preserves_menu_after_expiry(self):
        before = self.new_preview()
        with self.store.connect() as db:
            db.execute("UPDATE previews SET photo=x'',photo_id='' WHERE id=?", (before['id'],))
        await self.click('m:draft', 500)
        current = self.store.preview(OWNER)
        self.assertNotEqual(current['message_id'], 500)
        self.api.message.assert_awaited_once()
        self.api.photo.assert_not_awaited()
        self.reset_api()
        await self.click(f"d:{current['id']}:close", current['message_id'])
        self.api.message.assert_not_awaited()
        self.api.photo.assert_not_awaited()
        self.assertIn(current['message_id'], self.deleted())
        self.now += self.store.ttl + 1
        await self.bot.delivery.cleanup_expired()
        self.assert_menu_untouched(500)

    async def test_draft_close_keeps_content_and_cancel_deletes_it(self):
        before = self.new_preview()
        await self.click('m:draft', 500)
        current = self.store.preview(OWNER)
        await self.click(f"d:{current['id']}:close", current['message_id'], photo=True)
        self.assertEqual(self.store.saved_preview(OWNER)['caption'], before['caption'])
        await self.click('m:draft', 500)
        current = self.store.preview(OWNER)
        await self.click(f"d:{current['id']}:cancel", current['message_id'], photo=True)
        with self.assertRaises(Stale): self.store.draft(OWNER)
        self.assertIn(current['message_id'], self.deleted())
        self.assert_menu_untouched(500)

    async def test_first_complete_draft_sends_only_independent_card(self):
        self.store.replace_draft(OWNER, self.product, 1)
        self.store.update_draft(OWNER, self.product, b'source')
        self.reset_api()
        with patch('canal_ofertas_bot.service.render_card', return_value=b'rendered'):
            await self.click('m:draft', 500)
        self.api.message.assert_not_awaited()
        self.api.photo.assert_awaited_once()
        self.assertNotEqual(self.store.preview(OWNER)['message_id'], 500)
        self.assertTrue(self.store.preview(OWNER)['template'])
        self.assert_menu_untouched(500)

    async def test_incomplete_draft_is_an_independent_text_card(self):
        self.store.replace_draft(OWNER, Product(URL, 'Título pendiente'), 1)
        await self.click('m:draft', 500)
        self.api.message.assert_awaited_once()
        self.api.photo.assert_not_awaited()
        self.assertNotEqual(self.store.preview(OWNER)['message_id'], 500)
        self.assertTrue(self.api.message.await_args.args[1].startswith('📝 Borrador'))
        self.assert_menu_untouched(500)

    async def test_draft_closes_old_editor_and_invalidates_previous_controls(self):
        before = self.new_preview()
        await self.click(f"p:{before['id']}:edit", before['message_id'], photo=True)
        editor = self.store.editor(OWNER)
        await self.click('m:draft', 500)
        self.assertIsNone(self.store.editor(OWNER))
        self.assertIn(editor['prompt'], self.deleted())
        self.assertIn(before['message_id'], self.deleted())
        self.assert_menu_untouched(500)
        with self.assertRaises(Stale): self.store.preview(OWNER, before['id'], before['message_id'])

    async def test_invalid_input_disappears_and_exact_warning_is_shown(self):
        await self.prepare()
        await self.text('Mira esta oferta: ' + URL)
        self.assertIn(self.serial - 1, self.deleted())
        warning = self.store.preparation(OWNER)['aux'][0]
        self.assertEqual(self.api.message.await_args.args[1], URL_WARNING)
        self.assertEqual(URL_WARNING, '⚠️ Envía solo un enlace HTTPS de amazon.es o un acortador oficial admitido.')
        self.assertNotIn(500, self.deleted())
        self.assertNotIn(warning, self.deleted())

    async def test_new_invalid_input_replaces_previous_warning(self):
        await self.prepare()
        await self.text('Hola')
        old = self.store.preparation(OWNER)['aux'][0]
        await self.text('Otra entrada no válida')
        self.assertIn(old, self.deleted())
        self.assertEqual(len(self.store.preparation(OWNER)['aux']), 1)

    async def test_prepare_close_removes_screen_and_warning_preserving_existing_data(self):
        post = self.new_post()
        draft = self.new_preview()
        await self.prepare()
        await self.text('Hola')
        warning = self.store.preparation(OWNER)['aux'][0]
        await self.click('u:close', 500)
        self.assertTrue({500, warning} <= self.deleted())
        self.assertIsNone(self.store.preparation(OWNER))
        self.assertEqual(self.store.preview(OWNER)['caption'], draft['caption'])
        self.assertEqual(self.store.get_post(post['id']), post)

    async def test_prepare_back_restores_menu_and_removes_warning(self):
        await self.prepare()
        await self.text('Hola')
        warning = self.store.preparation(OWNER)['aux'][0]
        self.reset_api()
        await self.click('m:home', 500)
        self.assertIn(warning, self.deleted())
        self.assertNotIn(500, self.deleted())
        self.assertIsNone(self.store.preparation(OWNER))
        self.api.message.assert_not_awaited()
        self.assertIn('Canal Ofertas Bot', self.edits()[-1].kwargs['text'])

    async def test_cleanup_survives_restart(self):
        await self.prepare()
        await self.text('Hola')
        warning = self.store.preparation(OWNER)['aux'][0]
        restarted = Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now)
        self.bot = Service(self.config, restarted, self.api)
        await self.click('u:close', 500)
        self.assertTrue({500, warning} <= self.deleted())
        self.assertIsNone(restarted.preparation(OWNER))

    async def test_other_close_does_not_clear_active_preparation(self):
        await self.prepare()
        await self.text('Hola')
        session = self.store.preparation(OWNER)
        await self.click('u:close', 999)
        self.assertEqual(self.store.preparation(OWNER), session)
        self.assertNotIn(session['aux'][0], self.deleted())

    async def test_valid_link_cleans_before_extraction_even_when_data_is_missing(self):
        for complete in (False, True):
            with self.subTest(complete=complete):
                await self.prepare()
                await self.text('Hola')
                warning = self.store.preparation(OWNER)['aux'][0]
                async def extracted(value):
                    self.assertTrue({500, warning} <= self.deleted())
                    self.assertIsNone(self.store.preparation(OWNER))
                    return self.product if complete else Product(URL)
                self.bot.amazon = type('AmazonStub', (), {})()
                self.bot.amazon.extract = AsyncMock(side_effect=extracted)
                self.bot.make_preview = AsyncMock()
                await self.text(URL)
                self.assertTrue({500, warning} <= self.deleted())
                self.assertIsNone(self.store.preparation(OWNER))
                self.bot.amazon.extract.assert_awaited_once_with(URL)
                if complete:
                    # Product needs its image URL for a full extracted preview.
                    self.assertIn('Completa los datos', self.api.message.await_args.args[1])
                self.reset_api()

    async def test_valid_link_cleans_before_full_preview_and_extraction_error(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                await self.prepare()
                await self.text('Hola')
                warning = self.store.preparation(OWNER)['aux'][0]
                self.product.image_url = 'https://m.media-amazon.com/images/I/example.jpg'
                self.bot.amazon = type('AmazonStub', (), {})()
                self.bot.amazon.extract = AsyncMock(side_effect=ValueError('Extracción incompleta')) if fail else AsyncMock(return_value=self.product)
                self.bot.make_preview = AsyncMock()
                await self.text(URL)
                self.assertTrue({500, warning} <= self.deleted())
                self.assertIsNone(self.store.preparation(OWNER))
                if not fail: self.bot.make_preview.assert_awaited_once()
                self.reset_api()

    async def test_invalid_input_without_prepare_is_removed_and_following_valid_link_cleans_warning(self):
        await self.text('Hola')
        warning = self.store.preparation(OWNER)['aux'][0]
        self.bot.amazon = type('AmazonStub', (), {'extract': AsyncMock(return_value=Product(URL))})()
        await self.text(URL)
        self.assertIn(warning, self.deleted())
        self.assertIsNone(self.store.preparation(OWNER))

    async def test_image_prompts_have_back_on_left_for_new_draft(self):
        for action in ('change', 'replace'):
            with self.subTest(action=action):
                before = self.new_preview()
                with self.store.connect() as db:
                    db.execute('UPDATE previews SET template=1 WHERE id=?', (before['id'],))
                await self.click(f"p:{before['id']}:edit", before['message_id'], photo=True)
                editor = self.store.editor(OWNER)
                await self.click(f"e:{editor['preview_id']}:{action}", editor['prompt'])
                update = self.edits()[-1]
                self.assertEqual([b['text'] for b in update.kwargs['reply_markup']['inline_keyboard'][-1]], ['⬅️ - Volver', '❌ - Cerrar'])
                await self.click(f"e:{editor['preview_id']}:back", editor['prompt'])
                self.assertNotIn('mode', self.store.editor(OWNER)['state'])
                self.assertEqual(self.store.preview(OWNER, editing=True)['caption'], before['caption'])
                self.assertTrue(self.edits()[-1].kwargs['text'].startswith('✏️ Para editar'))
                self.assertEqual(self.edits()[-1].kwargs['message_id'], editor['prompt'])
                await self.click(f"e:{editor['preview_id']}:close", editor['prompt'])

    async def test_image_prompt_back_for_scheduled_post_preserves_post(self):
        for action in ('change', 'replace'):
            with self.subTest(action=action):
                post = self.new_post()
                with self.store.connect() as db:
                    db.execute('UPDATE posts SET template=1 WHERE id=?', (post['id'],))
                post = self.store.get_post(post['id'])
                await self.q(self.view(post, 'preview', {'media': True}), 'edit')
                editor = self.latest_view('editor')
                await self.q(editor, action)
                self.assertEqual([b['text'] for b in self.edits()[-1].kwargs['reply_markup']['inline_keyboard'][-1]], ['⬅️ - Volver', '❌ - Cerrar'])
                await self.q(editor, 'back')
                self.assertNotIn('mode', self.store.scheduled_view(OWNER, editor['token'])['state'])
                self.assertEqual(self.store.get_post(post['id']), post)
                self.assertEqual(self.edits()[-1].kwargs['message_id'], editor['message_id'])
                await self.q(editor, 'close')

    async def test_repeated_product_warnings_removed_on_close_and_cancel_only_new_draft_is_affected(self):
        for wording in ('publicado', 'programado'):
            for action in ('close', 'discard'):
                with self.subTest(wording=wording, action=action):
                    post = self.new_post()
                    draft = self.new_preview()
                    warning = self.serial + 1000
                    self.store.add_repeat_warning(OWNER, warning)
                    await self.click(f"p:{draft['id']}:{action}", draft['message_id'], photo=True)
                    self.assertTrue({draft['message_id'], warning} <= self.deleted())
                    self.assertEqual(self.store.repeat_warning_messages(OWNER), [])
                    self.assertEqual(self.store.get_post(post['id']), post)
                    if action == 'close':
                        self.assertEqual(self.store.saved_preview(OWNER)['caption'], draft['caption'])
                    else:
                        with self.assertRaises(Stale): self.store.draft(OWNER)

    async def test_foreign_warning_and_prepare_are_preserved(self):
        self.store.set_preparation(OTHER, 999)
        self.store.add_preparation_warning(OTHER, 1000)
        draft = self.new_preview()
        self.store.add_repeat_warning(OTHER, 1001)
        await self.click(f"p:{draft['id']}:close", draft['message_id'], photo=True)
        self.assertEqual(self.store.preparation(OTHER)['aux'], [1000])
        self.assertEqual(self.store.repeat_warning_messages(OTHER), [1001])
        self.assertFalse(self.deleted(OTHER))

    def test_url_validation_uses_approved_warning(self):
        for value in ('http://amazon.es/x', 'Hola ' + URL, 'https://example.com', URL+' texto'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, '^⚠️ Envía solo un enlace HTTPS de amazon.es'):
                    validate_url(value)

    async def test_reopening_draft_replaces_only_old_card_and_keeps_one_active_preview(self):
        before = self.new_preview()
        await self.click('m:draft', 500)
        first = self.store.preview(OWNER)
        await self.click('m:draft', 500)
        second = self.store.preview(OWNER)
        self.assertNotEqual(second['message_id'], first['message_id'])
        self.assertIn(first['message_id'], self.deleted())
        self.assertNotIn(second['message_id'], self.deleted())
        self.assertEqual(second['caption'], before['caption'])
        self.assertEqual(second['expires'], before['expires'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM previews WHERE owner=?', (OWNER,)).fetchone()[0], 1)
        self.assert_menu_untouched(500)

    async def test_no_draft_notice_replaces_menu_and_can_return(self):
        await self.click('m:draft', 500)
        self.api.message.assert_not_awaited()
        edit = self.edits()[-1]
        self.assertEqual(edit.kwargs['message_id'], 500)
        self.assertEqual(edit.kwargs['text'], '📝 No hay ningún borrador pendiente.')
        self.assertEqual(edit.kwargs['reply_markup']['inline_keyboard'], ui.navigation())
        await self.click('u:home', 500)
        self.assertEqual(self.edits()[-1].kwargs['message_id'], 500)

    async def test_pending_refresh_replaces_only_pending_cards(self):
        first, second = self.new_post(), self.new_post()
        await self.bot.show_pending()
        with self.store.connect() as db:
            cards = {row['post_id']: row['message_id'] for row in db.execute("SELECT * FROM scheduled_views WHERE kind='card'")}
        self.now += 3601
        self.store.claim(OWNER, first['id'])
        self.store.finish(first['id'], 'sent', 1)
        self.reset_api()
        await self.bot.show_pending()
        self.assertNotIn(cards[first['id']], self.deleted())
        self.assertIn(cards[second['id']], self.deleted())
        self.assertEqual(self.api.photo.await_count, 1)

    async def test_refresh_cleans_all_old_pending_confirmations_and_queries(self):
        posts = [self.new_post() for _ in range(3)]
        confirmations = []
        for post in posts:
            await self.bot.schedule_confirmation(post['id'])
            confirmations.append(self.latest_view('confirmation')['message_id'])
        # Put one card into each terminal state before the first consultation.
        self.now += 3601
        self.store.claim(OWNER, posts[0]['id'])
        self.store.finish(posts[0]['id'], 'sent', 1)
        self.store.cancel_post(OWNER, posts[1]['id'])
        draft = self.new_preview()
        await self.bot.show_pending()
        first_query = self.latest_view('card')
        self.assertIn(confirmations[2], self.deleted())
        self.assertTrue(set(confirmations[:2]).isdisjoint(self.deleted()))
        # A confirmation from another moment above another menu is also removed.
        await self.bot.schedule_confirmation(posts[2]['id'])
        another_confirmation = self.latest_view('confirmation')['message_id']
        self.reset_api()
        await self.click('m:pending', 500)
        self.assertEqual(self.deleted(), {first_query['message_id'], another_confirmation})
        self.assert_menu_untouched(500)
        self.assertEqual(self.store.preview(OWNER)['message_id'], draft['message_id'])
        self.assertEqual(self.api.photo.await_count, 1)
        self.assertEqual(self.latest_view('card')['post_id'], posts[2]['id'])
        # Every pending publication is covered, independent of its position.
        additional = self.new_post()
        await self.bot.schedule_confirmation(additional['id'])
        old_confirmation = self.latest_view('confirmation')['message_id']
        old_query = self.latest_view('card')['message_id']
        self.reset_api()
        await self.click('m:pending', 900)
        self.assertEqual(self.deleted(), {old_query, old_confirmation})
        self.assertEqual(self.api.photo.await_count, 2)

    async def test_empty_pending_preserves_terminal_cards(self):
        post = self.new_post()
        await self.bot.show_pending()
        card = self.latest_view('card')
        self.now += 3601
        self.store.claim(OWNER, post['id'])
        self.store.finish(post['id'], 'sent', 1)
        self.reset_api()
        await self.bot.show_pending()
        self.assertNotIn(card['message_id'], self.deleted())
        self.assertIn('No hay publicaciones programadas pendientes', self.api.message.await_args.args[1])

    async def test_full_preview_close_keeps_menu_and_programmed_posts(self):
        post = self.new_post()
        before = self.new_preview()
        await self.click('m:draft', 500)
        card = self.store.preview(OWNER)
        await self.click(f"d:{card['id']}:open", card['message_id'], photo=True)
        self.reset_api()
        await self.click(f"p:{card['id']}:close", card['message_id'], photo=True)
        self.api.message.assert_not_awaited()
        self.api.photo.assert_not_awaited()
        self.assertEqual(self.store.saved_preview(OWNER)['caption'], before['caption'])
        self.assertEqual(self.store.get_post(post['id']), post)
        self.assert_menu_untouched(500)

    async def test_independent_card_expiry_preserves_menu(self):
        before = self.new_preview()
        await self.click('m:draft', 500)
        card = self.store.preview(OWNER)
        self.now += self.store.ttl + 1
        await self.bot.delivery.cleanup_expired()
        self.assertIn(card['message_id'], self.deleted())
        self.assert_menu_untouched(500)


class HelpPreservationTests(unittest.TestCase):
    def test_both_help_messages_remain_exactly_as_before(self):
        expected = {'HELP': '7a59ea64d30aae690506ef90efa52ec45d639fe0a2519cc78df8920121591341', 'HELP_BODY': 'f83922ddffd9a41e2b9314a0e6a84312109198a761e7ab41607a61f9c5a5bc05'}
        self.assertEqual(hashlib.sha256(HELP.encode()).hexdigest(), expected['HELP'])
        self.assertEqual(hashlib.sha256(ui.HELP_BODY.encode()).hexdigest(), expected['HELP_BODY'])
