"""Lifecycle cards update in place without additional publication/cancellation messages."""
from pathlib import Path
import unittest

import test_menus_scheduled as helpers
from test_menus_scheduled import OWNER, OTHER, CHANNEL
from canal_ofertas_bot.delivery import Delivery
from canal_ofertas_bot.storage import Store, Stale
from canal_ofertas_bot.telegram import TelegramError


class CardStates(unittest.IsolatedAsyncioTestCase):
    setUp = helpers.ScheduledTests.setUp
    sent = helpers.ScheduledTests.sent
    new_preview = helpers.ScheduledTests.new_preview
    new_post = helpers.ScheduledTests.new_post
    view = helpers.ScheduledTests.view
    click = helpers.ScheduledTests.click
    q = helpers.ScheduledTests.q
    latest_view = helpers.ScheduledTests.latest_view

    def expected(self, heading):
        return (heading + '\n05/11/2026 14:00 (Madrid)\nUno dos tres cuatro cinco…\n'
                f'Preparado por: {OWNER}\nDestino: {CHANNEL}')

    def card_ids(self, post_id):
        with self.store.connect() as db:
            return {r[0] for r in db.execute('SELECT message_id FROM publication_cards WHERE post_id=?', (post_id,))}

    def assert_terminal_calls(self, ids, heading):
        edits = [c for c in self.api.call.await_args_list if c.args[0] == 'editMessageCaption']
        self.assertEqual({c.kwargs['message_id'] for c in edits}, ids)
        for call in edits:
            self.assertEqual(call.kwargs['caption'], self.expected(heading))
            self.assertEqual(call.kwargs['reply_markup'], {'inline_keyboard': []})
            self.assertEqual(call.kwargs['caption_entities'], [])
        self.api.message.assert_not_awaited()

    async def test_manual_programming_and_pending_list_share_exact_photo_card(self):
        preview = self.new_preview()
        await helpers.ScheduledTests.text(self, '/programar 05/11/2026 14:00')
        edit = helpers.ScheduledTests.last_edit(self)
        self.assertEqual(edit.args[0], 'editMessageCaption')
        self.assertEqual(edit.kwargs['message_id'], preview['message_id'])
        self.assertEqual(edit.kwargs['caption'], self.expected('🕒 Pendiente'))
        self.api.message.assert_not_awaited()
        await self.bot.show_pending()
        self.assertEqual(self.api.photo.await_args.args[2], self.expected('🕒 Pendiente'))

    async def test_delivery_updates_every_available_photo_including_open_picker(self):
        post = self.new_post()
        await self.bot.show_pending()
        await self.bot.show_pending()
        await self.q(self.latest_view('card'), 'open')
        await self.q(self.latest_view('preview'), 'schedule')
        ids = self.card_ids(post['id'])
        self.assertEqual(len(ids), 2)
        self.api.call.reset_mock()
        self.api.photo.reset_mock()
        self.now = post['due'] + 1
        await self.bot.delivery.tick()
        self.assert_terminal_calls(ids, '✅ Publicada')
        self.api.photo.assert_awaited_once_with(CHANNEL, post['photo_id'], post['caption'], post['entities'])
        await self.bot.delivery.tick()
        self.api.photo.assert_awaited_once()

    async def test_cancel_updates_all_cards_and_keeps_another_publication(self):
        post = self.new_post()
        await self.bot.show_pending()
        await self.bot.show_pending()
        card = self.latest_view('card')
        ids = self.card_ids(post['id'])
        other = self.new_post()
        self.api.call.reset_mock()
        await self.q(card, 'cancel')
        self.assert_terminal_calls(ids, '🚫 Cancelada')
        self.assertEqual(self.store.get_post(post['id'])['status'], 'cancelled')
        self.assertEqual(self.store.get_post(other['id'])['status'], 'pending')
        self.assertEqual(self.store.get_post(post['id'])['scheduled_due'], post['due'])
        with self.assertRaises(Stale):
            self.store.scheduled_post(post['id'], post['revision'], OWNER, (OWNER,))

    async def test_publish_now_keeps_the_confirmed_schedule_date(self):
        post = self.new_post()
        await self.bot.show_pending()
        await self.q(self.latest_view('card'), 'open')
        preview = self.latest_view('preview')
        ids = self.card_ids(post['id'])
        self.api.call.reset_mock()
        self.api.photo.reset_mock()
        await self.q(preview, 'publish')
        self.assert_terminal_calls(ids, '✅ Publicada')
        updated = self.store.get_post(post['id'])
        self.assertEqual(updated['scheduled_due'], post['due'])
        self.assertEqual(updated['due'], self.now)
        self.now = post['due'] + 1
        await self.bot.delivery.tick()
        self.api.photo.assert_awaited_once()

    async def test_retry_after_restart_only_updates_the_card_that_failed(self):
        post = self.new_post()
        await self.bot.show_pending()
        ids = self.card_ids(post['id'])
        retry_id = min(ids)
        failed = False
        def response(method, **kwargs):
            nonlocal failed
            if method == 'editMessageCaption' and kwargs['message_id'] == retry_id and not failed:
                failed = True
                raise TelegramError(429, retry_after=1)
            return True
        self.api.call.side_effect = response
        self.api.photo.reset_mock()
        self.now = post['due'] + 1
        await self.bot.delivery.tick()
        self.assertEqual(len(self.store.notices(OWNER)), 1)
        self.assertEqual({c['message_id'] for c in self.store.terminal_cards(post['id'], 'sent')}, {retry_id})
        self.api.call.reset_mock()
        restarted = Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now)
        await Delivery(self.config, restarted, self.api).tick()
        edits = [c for c in self.api.call.await_args_list if c.args[0] == 'editMessageCaption']
        self.assertEqual([c.kwargs['message_id'] for c in edits], [retry_id])
        self.assertEqual(edits[0].kwargs['caption'], self.expected('✅ Publicada'))
        self.api.message.assert_not_awaited()
        self.api.photo.assert_awaited_once()

    async def test_deleted_card_does_not_block_other_updates_or_create_message(self):
        post = self.new_post()
        await self.bot.show_pending()
        ids = self.card_ids(post['id'])
        gone = min(ids)
        def response(method, **kwargs):
            if method == 'editMessageCaption' and kwargs['message_id'] == gone:
                raise TelegramError(400)
            return True
        self.api.call.side_effect = response
        self.now = post['due'] + 1
        await self.bot.delivery.send(post['id'])
        await self.bot.delivery.notify()
        self.assertEqual(self.store.notices(OWNER), [])
        self.assertEqual(self.card_ids(post['id']), ids - {gone})
        self.api.message.assert_not_awaited()

    async def test_v4_database_migration_keeps_all_existing_cards_and_schedule(self):
        post = self.new_post()
        await self.bot.show_pending()
        await self.bot.show_pending()
        ids = self.card_ids(post['id'])
        original_id = min(ids)
        with self.store.connect() as db:
            db.execute('DROP TABLE publication_cards')
            db.execute('CREATE TABLE publication_cards(post_id TEXT PRIMARY KEY,chat_id INTEGER,message_id INTEGER)')
            db.execute('INSERT INTO publication_cards VALUES (?,?,?)', (post['id'], OWNER, original_id))
            db.execute('ALTER TABLE posts DROP COLUMN scheduled_due')
            db.execute('PRAGMA user_version=4')
        upgraded = Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now)
        migrated = upgraded.get_post(post['id'])
        for key, value in post.items():
            self.assertEqual(migrated[key], value)
        self.store = upgraded
        self.assertEqual(self.card_ids(post['id']), ids)
        with upgraded.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 6)
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    async def test_card_bound_after_delivery_gets_terminal_state_without_republishing(self):
        post = self.new_post()
        pending_view = self.store.add_scheduled_view(OWNER, post['id'], post['revision'], 'card', {'media': True}, (OWNER,))
        self.now = post['due'] + 1
        await self.bot.delivery.tick()
        self.assertIsNotNone(self.store.get_post(post['id']))
        self.store.bind_scheduled_view(OWNER, pending_view, 9999)
        self.api.call.reset_mock()
        await self.bot.delivery.tick()
        self.assert_terminal_calls({9999}, '✅ Publicada')
        self.api.photo.assert_awaited_once()
        self.assertIsNone(self.store.get_post(post['id']))

    async def test_reopened_borrador_ignores_legacy_confirmation_text(self):
        post = self.new_post()
        view = self.view(post, 'confirmation', {'media': True, 'confirmation_text': '✅ Reprogramada para una fecha antigua.'})
        await self.q(view, 'open')
        await self.q(self.latest_view('preview'), 'close')
        self.assertEqual(self.api.call.await_args.kwargs['caption'], self.expected('🕒 Pendiente'))
        self.assertEqual(self.api.call.await_args.kwargs['message_id'], view['message_id'])

    async def test_manual_cancel_updates_cards_without_extra_acknowledgement(self):
        post = self.new_post()
        await self.bot.show_pending()
        ids = self.card_ids(post['id'])
        self.api.call.reset_mock()
        await helpers.ScheduledTests.text(self, '/anular ' + post['id'])
        self.assert_terminal_calls(ids, '🚫 Cancelada')
        self.assertEqual(self.store.get_post(post['id'])['status'], 'cancelled')

    async def test_cancellation_updates_legacy_text_card_in_place(self):
        post = self.new_post()
        card = self.view(post, 'card', {'media': False})
        await self.q(card, 'cancel')
        self.api.call.assert_any_await('editMessageText', chat_id=OWNER, message_id=card['message_id'],
                                      text=self.expected('🚫 Cancelada'), entities=[], reply_markup={'inline_keyboard': []})
        self.api.message.assert_not_awaited()

    async def test_late_preview_edit_replaces_terminal_caption_then_is_refreshed(self):
        post = self.new_post()
        with self.store.connect() as db:
            message_id = db.execute('SELECT message_id FROM publication_cards WHERE post_id=?', (post['id'],)).fetchone()[0]
        in_flight = self.store.add_scheduled_view(OWNER, post['id'], post['revision'], 'preview', {'media': True}, (OWNER,))
        self.now = post['due'] + 1
        await self.bot.delivery.tick()
        # Simulate a preview edit whose Telegram response arrives after the terminal update.
        self.store.bind_scheduled_view(OWNER, in_flight, message_id)
        self.api.call.reset_mock()
        await self.bot.delivery.tick()
        self.assert_terminal_calls({message_id}, '✅ Publicada')
        self.api.photo.assert_awaited_once()

    async def test_late_binding_during_terminal_response_is_not_marked_complete(self):
        post = self.new_post()
        with self.store.connect() as db:
            message_id = db.execute('SELECT message_id FROM publication_cards WHERE post_id=?', (post['id'],)).fetchone()[0]
        view = self.store.add_scheduled_view(OWNER, post['id'], post['revision'], 'preview', {'media': True}, (OWNER,))
        bound = False
        def response(method, **kwargs):
            nonlocal bound
            if method == 'editMessageCaption' and not bound:
                bound = True
                self.store.bind_scheduled_view(OWNER, view, message_id)
            return True
        self.api.call.side_effect = response
        self.now = post['due'] + 1
        await self.bot.delivery.tick()
        self.assertEqual(len(self.store.notices(OWNER)), 1)
        self.assertEqual(len(self.store.terminal_cards(post['id'], 'sent')), 1)
        self.api.call.reset_mock()
        await self.bot.delivery.tick()
        self.assert_terminal_calls({message_id}, '✅ Publicada')
        self.api.photo.assert_awaited_once()
