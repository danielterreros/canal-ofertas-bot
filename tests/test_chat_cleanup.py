"""UI lifecycles and commands, with simulated Telegram and persistent SQLite."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime

import test_menus_scheduled as fixtures
import test_navigation_cleanup as navigation
from canal_ofertas_bot.service import Service
from canal_ofertas_bot.storage import Store, Stale
from canal_ofertas_bot.telegram import TelegramError
from canal_ofertas_bot.domain import Product

OWNER, OTHER, CHANNEL = fixtures.OWNER, fixtures.OTHER, fixtures.CHANNEL


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ScheduledTests.setUp
    sent = fixtures.ScheduledTests.sent
    new_preview = fixtures.ScheduledTests.new_preview
    new_post = fixtures.ScheduledTests.new_post
    click = fixtures.ScheduledTests.click
    q = fixtures.ScheduledTests.q
    latest_view = fixtures.ScheduledTests.latest_view
    reset_api = navigation.NavigationTests.reset_api
    deleted = navigation.NavigationTests.deleted
    edits = navigation.NavigationTests.edits

    async def command(self, text, message_id=900, actor=OWNER):
        await self.bot.handle({'message': {'message_id': message_id, 'text': text,
                              'from': {'id': actor}, 'chat': {'id': actor, 'type': 'private'}}})

    async def test_start_replaces_menu_only_after_new_one_is_visible(self):
        draft = self.new_preview()
        post = self.new_post(OTHER)
        await self.command('/start', 900)
        first = self.store.screen_messages(OWNER, 'home')[0]
        self.reset_api()
        order = []
        def sent(*args, **kwargs):
            order.append('sent')
            return self.sent(*args, **kwargs)
        def call(method, **kwargs):
            if method == 'deleteMessage': order.append(kwargs['message_id'])
            return True
        self.api.message.side_effect = sent
        self.api.call.side_effect = call
        await self.command('/start', 901)
        self.assertIn(first, self.deleted())
        self.assertIn(901, self.deleted())
        self.assertNotIn(draft['message_id'], self.deleted())
        self.assertLess(order.index('sent'), order.index(first))
        self.assertEqual(self.store.preview(OWNER)['caption'], draft['caption'])
        self.assertEqual(self.store.get_post(post['id']), post)
        self.assertEqual(len(self.store.screen_messages(OWNER, 'home')), 1)

    async def test_query_screens_replace_only_same_section_and_follow_navigation(self):
        await self.command('/start')
        menu = self.store.screen_messages(OWNER, 'home')[0]
        await self.command('/status', 901)
        previous_status = self.store.screen_messages(OWNER, '📊 - Estado')[0]
        self.reset_api()
        await self.click('m:status', menu)
        self.assertIn(previous_status, self.deleted())
        self.assertNotIn(menu, self.deleted())
        self.assertFalse(self.store.screen_messages(OWNER, 'home'))
        self.assertEqual(self.store.screen_messages(OWNER, '📊 - Estado'), [menu])
        await self.click('m:home', menu)
        self.assertEqual(self.store.screen_messages(OWNER, 'home'), [menu])
        self.assertFalse(self.store.screen_messages(OWNER, '📊 - Estado'))

    async def test_consultations_are_unique_and_survive_restart(self):
        for command, section in [('/help', '❓ - Ayuda'), ('/id', 'id'), ('/status', '📊 - Estado')]:
            with self.subTest(command=command):
                await self.command(command)
                old = self.store.screen_messages(OWNER, section)[0]
                self.bot = Service(self.config, Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now), self.api)
                self.reset_api()
                await self.command(command, 901)
                self.assertIn(old, self.deleted())
                self.assertIn(901, self.deleted())
                self.assertEqual(len(self.store.screen_messages(OWNER, section)), 1)

    async def test_every_button_section_reuses_screen_and_retires_duplicate(self):
        for action, section in [('users','👥 - Usuarios'),('about','ℹ️ - Acerca de'),
                                ('version','🔢 - Versión'),('help','❓ - Ayuda'),
                                ('status','📊 - Estado'),('draft','📝 - Borrador')]:
            with self.subTest(action=action):
                await self.bot.menu_action(action)
                previous = self.store.screen_messages(OWNER, section)[0]
                self.reset_api()
                await self.click('m:' + action, 500)
                self.assertIn(previous, self.deleted())
                self.assertNotIn(500, self.deleted())
                self.api.message.assert_not_awaited()
                self.assertEqual(self.store.screen_messages(OWNER, section), [500])
        await self.click('u:close', 500)
        self.assertFalse(self.store.screen_messages(OWNER, '📝 - Borrador'))

    async def test_failed_command_or_invalid_command_keeps_input_and_error(self):
        for text in ['/noexiste', '/editar precio', '/programar incorrecto', '/anular inexistente', '/datos']:
            with self.subTest(text=text):
                self.reset_api()
                await self.command(text)
                self.assertNotIn(900, self.deleted())
                self.api.message.assert_awaited()

    async def test_failure_to_send_replacement_keeps_previous_menu_and_command(self):
        await self.command('/start')
        menu = self.store.screen_messages(OWNER, 'home')[0]
        self.reset_api()
        def send(text_chat, text, *args, **kwargs):
            if text.startswith('🔥'): raise TelegramError(503)
            return self.sent(text_chat, text, *args, **kwargs)
        self.api.message.side_effect = send
        await self.command('/start', 901)
        self.assertNotIn(901, self.deleted())
        self.assertNotIn(menu, self.deleted())
        self.assertEqual(self.store.screen_messages(OWNER, 'home'), [menu])

    async def test_failed_screen_deletion_is_retained_for_next_cleanup(self):
        await self.command('/id')
        old = self.store.screen_messages(OWNER, 'id')[0]
        def fail_delete(method, **kwargs):
            if method == 'deleteMessage' and kwargs['message_id'] == old: raise TelegramError(429)
            return True
        self.api.call.side_effect = fail_delete
        await self.command('/id', 901)
        self.assertIn(old, self.store.screen_messages(OWNER, 'id'))
        self.api.call.side_effect = None
        self.reset_api()
        await self.command('/id', 902)
        self.assertIn(old, self.deleted())
        self.assertEqual(len(self.store.screen_messages(OWNER, 'id')), 1)

    async def test_same_section_in_another_private_chat_is_preserved(self):
        await self.command('/id', actor=OTHER)
        other = self.store.screen_messages(OTHER, 'id')
        await self.command('/id')
        self.reset_api()
        await self.command('/id', 901)
        self.assertEqual(self.store.screen_messages(OTHER, 'id'), other)
        self.assertFalse(self.deleted(OTHER))

    async def test_multipart_data_replaces_whole_previous_consultation(self):
        product = Product(**self.product.to_dict())
        product.warnings = ['Advertencia ' + 'x' * 1600 for _ in range(5)]
        self.store.replace_draft(OWNER, product, 1)
        await self.command('/datos')
        first = self.store.screen_messages(OWNER, 'datos')
        self.assertGreater(len(first), 1)
        self.reset_api()
        await self.command('/datos', 901)
        self.assertTrue(set(first) <= self.deleted())
        self.assertEqual(self.store.draft(OWNER)['data']['title'], product.title)
        self.assertEqual(len(self.store.screen_messages(OWNER, 'datos')), len(first))

    async def test_previous_data_survives_partial_failed_replacement(self):
        self.store.replace_draft(OWNER, self.product, 1)
        await self.command('/datos')
        previous = self.store.screen_messages(OWNER, 'datos')
        self.reset_api()
        self.api.message.side_effect = TelegramError(503)
        await self.command('/datos', 901)
        self.assertEqual(self.store.screen_messages(OWNER, 'datos'), previous)
        self.assertFalse(set(previous) & self.deleted())
        self.assertNotIn(901, self.deleted())

    async def test_edit_notice_removed_by_preview_without_discarding_draft(self):
        self.new_preview()
        self.store.update_draft(OWNER, self.product, b'source')
        await self.command('/editar precio 150,00')
        notice = self.api.message.await_args
        self.assertIn('Dato guardado', notice.args[1])
        notice_id = self.serial
        self.assertIn(900, self.deleted())
        self.reset_api()
        with patch('canal_ofertas_bot.service.render_card', return_value=b'card'):
            await self.command('/previa', 901)
        self.assertIn(notice_id, self.deleted())
        self.assertIn(901, self.deleted())
        self.assertEqual(self.store.draft(OWNER)['data']['price'], '150.00')
        self.assertIsNotNone(self.store.preview(OWNER))

    async def test_scheduling_and_cancelling_keep_final_card_and_delete_commands(self):
        preview = self.new_preview()
        due = datetime.fromtimestamp(self.now + 3600, self.config.timezone).strftime('%d/%m/%Y %H:%M')
        await self.command('/programar ' + due)
        post = self.store.pending_all((OWNER,))[0]
        self.assertIn(900, self.deleted())
        self.assertNotIn(preview['message_id'], self.deleted())
        self.reset_api()
        await self.command('/anular ' + post['id'], 901)
        self.assertIn(901, self.deleted())
        self.assertNotIn(preview['message_id'], self.deleted())
        self.api.message.assert_not_awaited()
        self.assertIn('🚫 Cancelada', self.edits()[-1].kwargs['caption'])

    async def test_cancellation_without_available_card_has_visible_confirmation(self):
        post = self.new_post()
        with self.store.connect() as db: db.execute('DELETE FROM publication_cards WHERE post_id=?', (post['id'],))
        await self.command('/anular ' + post['id'])
        self.assertIn(900, self.deleted())
        self.assertIn(post['id'], self.api.message.await_args.args[1])
        self.assertIn('Cancelada', self.api.message.await_args.args[1])

    async def test_failed_cancellation_card_update_keeps_command_and_explains_saved_state(self):
        post = self.new_post()
        def fail_caption(method, **kwargs):
            if method == 'editMessageCaption': raise TelegramError(503)
            return True
        self.api.call.side_effect = fail_caption
        await self.command('/anular ' + post['id'])
        self.assertNotIn(900, self.deleted())
        self.assertEqual(self.store.get_post(post['id'])['status'], 'cancelled')
        self.assertIn('cancelación está guardada', self.api.message.await_args.args[1])

    async def test_discard_command_removes_draft_but_keeps_schedules(self):
        post = self.new_post()
        draft = self.new_preview()
        await self.command('/cancelar')
        self.assertIn(900, self.deleted())
        self.assertIn(draft['message_id'], self.deleted())
        self.assertEqual(self.store.get_post(post['id']), post)
        self.assertIn('Borrador descartado', self.api.message.await_args.args[1])

    async def test_pending_command_keeps_terminal_cards_and_cleans_empty_notice(self):
        await self.command('/programadas')
        notice = self.store.screen_messages(OWNER, '📅 - Programadas')[0]
        first, second = self.new_post(), self.new_post()
        await self.bot.schedule_confirmation(first['id'])
        confirmation = self.latest_view('confirmation')['message_id']
        self.store.cancel_post(OWNER, first['id'])
        await self.bot.delivery.notify()
        self.reset_api()
        await self.command('/programadas', 901)
        self.assertIn(notice, self.deleted())
        self.assertIn(901, self.deleted())
        self.assertNotIn(confirmation, self.deleted())
        self.assertEqual(self.latest_view('card')['post_id'], second['id'])

    async def test_not_modified_navigation_is_successful(self):
        def unchanged(method, **kwargs):
            if method == 'editMessageText': raise TelegramError(400, not_modified=True)
            return True
        self.api.call.side_effect = unchanged
        await self.click('m:status', 500)
        self.api.message.assert_not_awaited()
        self.assertEqual(self.store.screen_messages(OWNER, '📊 - Estado'), [500])

    async def test_bare_commands_replace_usage_and_leave_pending_work_untouched(self):
        post = self.new_post()
        draft = self.new_preview()
        for command in ['/editar', '/programar', '/anular']:
            with self.subTest(command=command):
                self.reset_api()
                await self.command(command)
                previous = self.store.screen_messages(OWNER, 'usage:' + command)[0]
                self.assertIn(900, self.deleted())
                self.assertTrue(self.api.message.await_args.args[1].startswith('Uso: ' + command))
                # New instance, distant message IDs: the prior instructions are still found.
                self.bot = Service(self.config, Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now), self.api)
                self.reset_api()
                await self.command(command + '@testbot   ', 2000)
                self.assertIn(previous, self.deleted())
                self.assertIn(2000, self.deleted())
                self.assertEqual(len(self.store.screen_messages(OWNER, 'usage:' + command)), 1)
                self.assertEqual(self.store.get_post(post['id']), post)
                self.assertEqual(self.store.preview(OWNER), draft)

    async def test_bare_command_send_failure_preserves_input_and_previous_instructions(self):
        await self.command('/editar')
        previous = self.store.screen_messages(OWNER, 'usage:/editar')[0]
        self.reset_api()
        self.api.message.side_effect = TelegramError(503)
        await self.command('/editar', 901)
        self.assertNotIn(901, self.deleted())
        self.assertNotIn(previous, self.deleted())
        self.assertEqual(self.store.screen_messages(OWNER, 'usage:/editar'), [previous])

    async def test_successful_parameterized_command_retires_only_its_usage(self):
        post = self.new_post()
        self.new_preview()
        for command in ['/editar', '/programar', '/anular']:
            await self.command(command)
        edit_usage = self.store.screen_messages(OWNER, 'usage:/editar')[0]
        schedule_usage = self.store.screen_messages(OWNER, 'usage:/programar')[0]
        cancel_usage = self.store.screen_messages(OWNER, 'usage:/anular')[0]
        self.reset_api()
        await self.command('/editar precio 150,00', 901)
        self.assertIn(edit_usage, self.deleted())
        self.assertIn(901, self.deleted())
        self.assertNotIn(schedule_usage, self.deleted())
        self.assertNotIn(cancel_usage, self.deleted())
        # Generate an approved preview with the edited draft, then schedule it.
        self.store.update_draft(OWNER, Product(**self.store.draft(OWNER)['data']), b'source')
        with patch('canal_ofertas_bot.service.render_card', return_value=b'card'):
            await self.command('/previa', 902)
        due = datetime.fromtimestamp(self.now + 3600, self.config.timezone).strftime('%d/%m/%Y %H:%M')
        self.reset_api()
        await self.command('/programar ' + due, 903)
        self.assertIn(schedule_usage, self.deleted())
        self.assertIn(903, self.deleted())
        self.reset_api()
        await self.command('/anular ' + post['id'], 904)
        self.assertIn(cancel_usage, self.deleted())
        self.assertIn(904, self.deleted())

    async def test_invalid_parameters_keep_command_error_and_usage(self):
        for command, value in [('/editar', 'precio'), ('/programar', 'incorrecto'), ('/anular', 'sin id')]:
            with self.subTest(command=command):
                await self.command(command)
                usage = self.store.screen_messages(OWNER, 'usage:' + command)[0]
                self.reset_api()
                await self.command(command + ' ' + value, 901)
                self.assertNotIn(901, self.deleted())
                self.assertNotIn(usage, self.deleted())
                self.assertTrue(self.api.message.await_args.args[1].startswith('Uso: '))

    async def test_discard_confirmation_is_unique_across_restarts(self):
        post = self.new_post()
        self.new_preview()
        await self.command('/cancelar')
        previous = self.store.screen_messages(OWNER, 'confirmation:/cancelar')[0]
        self.bot = Service(self.config, Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now), self.api)
        self.reset_api()
        await self.command('/cancelar', 901)
        self.assertIn(previous, self.deleted())
        self.assertIn(901, self.deleted())
        self.assertEqual(len(self.store.screen_messages(OWNER, 'confirmation:/cancelar')), 1)
        self.assertEqual(self.store.get_post(post['id']), post)

    async def test_command_instructions_do_not_remove_another_users_screen(self):
        await self.command('/editar', actor=OTHER)
        other = self.store.screen_messages(OTHER, 'usage:/editar')
        await self.command('/editar')
        self.reset_api()
        await self.command('/editar', 901)
        self.assertFalse(self.deleted(OTHER))
        self.assertEqual(self.store.screen_messages(OTHER, 'usage:/editar'), other)

    async def editable_preview(self):
        self.store.replace_draft(OWNER, self.product, 1)
        self.store.update_draft(OWNER, self.product, b'source')
        with patch('canal_ofertas_bot.service.render_card', return_value=b'card'):
            await self.command('/previa')
        return self.store.preview(OWNER)

    async def test_preview_after_manual_edit_retires_invalidated_previous_card(self):
        first = await self.editable_preview()
        await self.command('/editar precio 150,00', 901)
        self.reset_api()
        with patch('canal_ofertas_bot.service.render_card', return_value=b'new-card'):
            await self.command('/previa', 902)
        self.assertIn(first['message_id'], self.deleted())
        self.assertIn(902, self.deleted())
        current = self.store.preview(OWNER)
        self.assertNotIn(current['message_id'], self.deleted())
        self.assertEqual(self.store.draft(OWNER)['data']['price'], '150.00')

    async def test_discard_also_removes_preview_invalidated_by_edit(self):
        first = await self.editable_preview()
        await self.command('/editar precio 150,00', 901)
        self.reset_api()
        await self.command('/cancelar', 902)
        self.assertIn(first['message_id'], self.deleted())
        self.assertIn(902, self.deleted())

    async def test_repeated_preview_without_edits_moves_to_bottom(self):
        first = await self.editable_preview()
        self.reset_api()
        with patch('canal_ofertas_bot.service.render_card', return_value=b'new-card'):
            await self.command('/previa', 901)
        self.api.photo.assert_awaited_once()
        current = self.store.preview(OWNER)
        self.assertNotEqual(current['message_id'], first['message_id'])
        self.assertIn(first['message_id'], self.deleted())
        self.assertNotIn(current['message_id'], self.deleted())
        self.assertIn(901, self.deleted())

    async def test_failed_replacement_retains_previous_preview_until_success(self):
        first = await self.editable_preview()
        await self.command('/editar precio 150,00', 901)
        self.reset_api()
        self.api.photo.side_effect = TelegramError(503)
        with patch('canal_ofertas_bot.service.render_card', return_value=b'new-card'):
            await self.command('/previa', 902)
        self.assertNotIn(first['message_id'], self.deleted())
        self.assertNotIn(902, self.deleted())
        self.api.photo.side_effect = self.sent
        with patch('canal_ofertas_bot.service.render_card', return_value=b'new-card'):
            await self.command('/previa', 903)
        self.assertIn(first['message_id'], self.deleted())
        self.assertIn(903, self.deleted())

    async def test_new_preview_preserves_previews_converted_to_scheduled_cards(self):
        first = await self.editable_preview()
        due = datetime.fromtimestamp(self.now + 3600, self.config.timezone).strftime('%d/%m/%Y %H:%M')
        await self.command('/programar ' + due, 901)
        scheduled = self.store.pending_all((OWNER,))[0]
        self.reset_api()
        await self.editable_preview()
        self.assertNotIn(first['message_id'], self.deleted())
        self.assertEqual(self.store.get_post(scheduled['id']), scheduled)

    async def test_upgrade_remembers_existing_preview_before_data_edit(self):
        first = await self.editable_preview()
        self.store.forget_screen(OWNER, first['message_id'])
        self.store = Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now)
        self.bot = Service(self.config, self.store, self.api)
        await self.command('/editar precio 150,00', 901)
        self.reset_api()
        with patch('canal_ofertas_bot.service.render_card', return_value=b'new-card'):
            await self.command('/previa', 902)
        self.assertIn(first['message_id'], self.deleted())

    async def test_command_preview_sends_before_removing_previous_card_and_command(self):
        first = await self.editable_preview()
        self.reset_api()
        events = []
        def photo(*args, **kwargs):
            events.append('new-preview')
            return self.sent(*args, **kwargs)
        def call(method, **kwargs):
            if method == 'deleteMessage': events.append(kwargs['message_id'])
            return True
        self.api.photo.side_effect = photo
        self.api.call.side_effect = call
        with patch('canal_ofertas_bot.service.render_card', return_value=b'new-card'):
            await self.command('/previa', 2000)
        self.assertLess(events.index('new-preview'), events.index(first['message_id']))
        self.assertLess(events.index(first['message_id']), events.index(2000))
        current = self.store.preview(OWNER)
        self.assertEqual(self.store.screen_messages(OWNER, 'draft-preview'), [current['message_id']])
        self.reset_api()
        await self.click(f"p:{first['id']}:publish", first['message_id'], photo=True)
        self.api.photo.assert_not_awaited()
        self.assertEqual(self.store.preview(OWNER)['id'], current['id'])

    async def test_failed_command_preview_without_edits_keeps_current_card_and_input(self):
        first = await self.editable_preview()
        self.reset_api()
        self.api.photo.side_effect = TelegramError(503)
        with patch('canal_ofertas_bot.service.render_card', return_value=b'new-card'):
            await self.command('/previa', 2000)
        self.assertNotIn(first['message_id'], self.deleted())
        self.assertNotIn(2000, self.deleted())
        self.assertEqual(self.store.preview(OWNER), first)

    async def test_default_preview_refresh_still_edits_in_place(self):
        first = await self.editable_preview()
        self.reset_api()
        with patch('canal_ofertas_bot.service.render_card', return_value=b'new-card'):
            await self.bot.make_preview()
        self.api.photo.assert_not_awaited()
        self.assertEqual(self.store.preview(OWNER)['message_id'], first['message_id'])
        self.assertNotIn(first['message_id'], self.deleted())

    async def assert_saved_preview_redisplayed(self, saved):
        self.reset_api()
        with patch('canal_ofertas_bot.service.render_card', return_value=b'unwanted-regeneration') as render:
            await self.command('/previa', 2000)
        current = self.store.preview(OWNER)
        for field in ['caption', 'entities', 'photo', 'template', 'source_photo', 'product_data']:
            self.assertEqual(current[field], saved[field], field)
        self.assertNotEqual(current['message_id'], saved['message_id'])
        self.assertIn(saved['message_id'], self.deleted())
        self.assertIn(2000, self.deleted())
        self.assertNotIn(current['message_id'], self.deleted())
        render.assert_not_called()
        if saved['photo'] or saved['photo_id']:
            # Telegram may return a fresh file_id when uploading the same saved image.
            self.assertEqual(self.api.photo.await_args.args[1], saved['photo_id'] or saved['photo'])
        else:
            self.api.photo.assert_not_awaited()
            self.assertFalse(current['photo_id'])
        return current

    async def test_command_preview_preserves_free_text_saved_through_editor(self):
        first = await self.editable_preview()
        await self.click(f"p:{first['id']}:edit", first['message_id'], photo=True)
        with patch('canal_ofertas_bot.editor_ui.render_card', return_value=b'edited-card'):
            await fixtures.ScheduledTests.text(self, 'Mi anuncio personalizado, guardado desde el editor.')
        saved = self.store.preview(OWNER)
        self.assertEqual(saved['caption'], 'Mi anuncio personalizado, guardado desde el editor.')
        await self.assert_saved_preview_redisplayed(saved)

    async def test_command_preview_preserves_synchronized_price_label_and_template(self):
        first = await self.editable_preview()
        await self.click(f"p:{first['id']}:edit", first['message_id'], photo=True)
        edited_text = first['caption'].replace('179,99', '150,00').replace('Oferta flash', 'Cupón: TECH')
        with patch('canal_ofertas_bot.editor_ui.render_card', return_value=b'edited-template'):
            await fixtures.ScheduledTests.text(self, edited_text)
        saved = self.store.preview(OWNER)
        self.assertEqual(saved['product_data']['price'], '150.00')
        self.assertIn('Cupón: TECH', saved['caption'])
        self.assertEqual(saved['photo'], b'edited-template')
        self.store = Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now)
        self.bot = Service(self.config, self.store, self.api)
        await self.assert_saved_preview_redisplayed(saved)

    async def test_command_preview_preserves_removed_photo_and_formatting(self):
        first = await self.editable_preview()
        await self.click(f"p:{first['id']}:edit", first['message_id'], photo=True)
        editor = self.store.editor(OWNER)
        await self.click(f"e:{editor['preview_id']}:remove", editor['prompt'])
        text_only = self.store.preview(OWNER)
        self.assertFalse(text_only['photo'] or text_only['photo_id'])
        await self.click(f"p:{text_only['id']}:edit", text_only['message_id'])
        self.serial += 1
        await self.bot.handle({'message': {'message_id': self.serial, 'text': 'Texto personalizado',
                              'entities': [{'type': 'bold', 'offset': 0, 'length': 5}],
                              'from': {'id': OWNER}, 'chat': {'id': OWNER, 'type': 'private'}}})
        saved = self.store.preview(OWNER)
        self.assertEqual(saved['entities'], [{'type': 'bold', 'offset': 0, 'length': 5}])
        await self.assert_saved_preview_redisplayed(saved)
        self.api.photo.assert_not_awaited()

    async def test_command_preview_preserves_image_only_and_custom_image(self):
        first = await self.editable_preview()
        # Same content persistence used by the image editor after replacing an image.
        custom = {**self.bot.content(first), 'caption': '', 'entities': [], 'photo': b'custom-image',
                  'photo_id': '', 'template': False, 'source_photo': b''}
        await self.bot.finish_draft_edit(custom)
        saved = self.store.preview(OWNER)
        self.assertFalse(saved['caption'])
        self.assertFalse(saved['template'])
        self.assertEqual(saved['photo'], b'custom-image')
        await self.assert_saved_preview_redisplayed(saved)

    async def test_unactivated_staged_content_never_replaces_saved_preview(self):
        first = await self.editable_preview()
        self.store.stage_preview(OWNER, CHANNEL, b'not-confirmed', 'Unconfirmed', [])
        self.assertEqual(self.store.saved_preview(OWNER)['id'], first['id'])
        await self.assert_saved_preview_redisplayed(first)
