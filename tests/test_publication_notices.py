"""Delivery acknowledgements and ephemeral duplicate warnings, without network requests."""
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_menus_scheduled as helpers
from test_menus_scheduled import OWNER, OTHER, CHANNEL
from canal_ofertas_bot import calendar_ui
from canal_ofertas_bot.delivery import Delivery
from canal_ofertas_bot.maintenance import backup, restore
from canal_ofertas_bot.network import NetworkError
from canal_ofertas_bot.post_ui import scheduled_card_text
from canal_ofertas_bot.storage import Store
from canal_ofertas_bot.telegram import Telegram, TelegramError

class PublicationNotices(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        helpers.ScheduledTests.setUp(self)
        self.saved_posts = {}

    sent = helpers.ScheduledTests.sent
    new_preview = helpers.ScheduledTests.new_preview
    def new_post(self, owner=OWNER):
        post = helpers.ScheduledTests.new_post(self, owner)
        self.saved_posts[post["id"]] = post
        return post

    view = helpers.ScheduledTests.view
    click = helpers.ScheduledTests.click
    q = helpers.ScheduledTests.q
    latest_view = helpers.ScheduledTests.latest_view

    def assert_card(self, post_id, message_id, chat_id=OWNER):
        post = self.store.get_post(post_id) or self.saved_posts.get(post_id)
        text = (scheduled_card_text(post, self.config.timezone, 'sent') if post
                else f'✅ Publicada en el destino {CHANNEL}. ID: {post_id}')
        self.api.call.assert_any_await('editMessageCaption', chat_id=chat_id, message_id=message_id,
                                      caption=text, caption_entities=[], reply_markup={'inline_keyboard': []})

    async def test_direct_publish_updates_original_photo_and_preserves_destination_content(self):
        preview = self.new_preview()
        await self.click(f"p:{preview['id']}:publish", preview['message_id'], photo=True)
        self.api.photo.assert_awaited_once_with(CHANNEL, preview['photo_id'], preview['caption'], preview['entities'])
        with self.store.connect() as db:
            post_id = db.execute('SELECT id FROM posts').fetchone()[0]
        self.assert_card(post_id, preview['message_id'])
        self.api.message.assert_not_awaited()
        await self.bot.delivery.notify()
        self.api.photo.assert_awaited_once()

    async def test_scheduled_delivery_remembers_card_after_restart(self):
        post = self.new_post()
        with self.store.connect() as db:
            original = db.execute('SELECT message_id FROM publication_cards WHERE post_id=?', (post['id'],)).fetchone()[0]
        self.now = post['due'] + 1
        restarted = Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now)
        await Delivery(self.config, restarted, self.api).tick()
        self.assert_card(post['id'], original)
        self.api.message.assert_not_awaited()
        self.api.photo.assert_awaited_once_with(CHANNEL, post['photo_id'], post['caption'], post['entities'])

    async def test_reprogram_uses_same_card_at_new_due_time(self):
        post = self.new_post()
        view = self.view(post, 'confirmation', {'media': True})
        await self.q(view, 'schedule')
        picker = self.latest_view('picker')
        for action, value in [('day', '2026-11-06'), ('hour', '15'), ('minute', '50')]:
            await self.click(f"r:{picker['token']}:{action}:{value}", picker['message_id'], photo=True)
            picker = self.latest_view('picker')
        await self.click(f"r:{picker['token']}:confirm:-", picker['message_id'], photo=True)
        self.saved_posts[post['id']] = self.store.get_post(post['id'])
        self.now = self.store.get_post(post['id'])['due'] + 1
        await self.bot.delivery.tick()
        self.assert_card(post['id'], view['message_id'])
        self.api.message.assert_not_awaited()
        self.api.photo.assert_awaited_once()

    async def test_publish_now_from_scheduled_draft_updates_that_draft(self):
        post = self.new_post()
        view = self.view(post, 'preview', {'media': True})
        await self.q(view, 'publish')
        self.assert_card(post['id'], view['message_id'])
        self.api.message.assert_not_awaited()
        self.now = post['due'] + 1
        await self.bot.delivery.tick()
        self.api.photo.assert_awaited_once()

    async def test_schedule_and_reschedule_confirmations_omit_price_notice(self):
        post = self.new_post()
        for reprogram in (False, True):
            await self.bot.schedule_confirmation(post['id'],
                                                 message_id=200, media=True)
            call = self.api.call.await_args
            self.assertEqual(len(call.kwargs['caption'].splitlines()), 5)
            self.assertNotIn('precios', call.kwargs['caption'])
            self.assertEqual(len(call.kwargs['reply_markup']['inline_keyboard'][0]), 2)

    async def sent_post(self):
        post = self.new_post()
        self.now = post['due'] + 1
        await self.bot.delivery.send(post['id'])
        return post

    async def test_missing_scheduled_card_does_not_create_another_message(self):
        post = await self.sent_post()
        self.api.call.side_effect = TelegramError(400)
        await self.bot.delivery.notify()
        self.api.message.assert_not_awaited()
        self.assertEqual(self.store.notices(OWNER), [])

    async def test_closed_original_card_keeps_schedule_without_new_message(self):
        post = self.new_post()
        with self.store.connect() as db:
            message_id = db.execute('SELECT message_id FROM publication_cards').fetchone()[0]
        await self.bot.remove(message_id)
        self.assertEqual(self.store.get_post(post['id'])['status'], 'pending')
        self.now = post['due'] + 1
        await self.bot.delivery.tick()
        self.api.message.assert_not_awaited()

    async def test_transient_edit_failure_retries_notice_without_republishing(self):
        post = await self.sent_post()
        self.api.call.side_effect = NetworkError('Conexión interrumpida', uncertain=True)
        await self.bot.delivery.notify()
        self.api.message.assert_not_awaited()
        self.assertEqual(len(self.store.notices(OWNER)), 1)
        self.api.call.side_effect = None
        await self.bot.delivery.notify()
        self.assertEqual(self.store.notices(OWNER), [])
        self.api.photo.assert_awaited_once()

    async def test_already_updated_card_is_success_without_duplicate_notice(self):
        await self.sent_post()
        self.api.call.side_effect = TelegramError(400, not_modified=True)
        await self.bot.delivery.notify()
        self.api.message.assert_not_awaited()
        self.assertEqual(self.store.notices(OWNER), [])

    async def test_delivery_failure_keeps_original_card_and_sends_error_notice(self):
        post = self.new_post()
        self.now = post['due'] + 1
        self.api.photo.side_effect = TelegramError(403)
        await self.bot.delivery.tick()
        self.api.call.assert_not_awaited()
        self.assertIn('Telegram rechazó', self.api.message.call_args.args[1])

    async def test_warning_registration_and_all_four_actions_both_repeat_types(self):
        for state in ('pending', 'sent'):
            for action in ('publish', 'schedule', 'edit', 'discard'):
                with self.subTest(state=state, action=action):
                    prior = self.new_post()
                    if state == 'sent':
                        self.now = prior['due'] + 1
                        self.store.claim(OWNER, prior['id'])
                        self.store.finish(prior['id'], 'sent', 99)
                    self.store.replace_draft(OWNER, self.product, 1)
                    self.store.update_draft(OWNER, self.product, photo=b'image')
                    with patch('canal_ofertas_bot.service.render_card', return_value=b'image'):
                        await self.bot.make_preview()
                    preview = self.store.preview(OWNER)
                    warning_id = self.store.repeat_warning_messages(OWNER)[0]
                    warning_text = self.api.message.call_args.args[1]
                    self.assertIn('ya está programado' if state == 'pending' else 'últimos 7 días', warning_text)
                    await self.click(f"p:{preview['id']}:{action}", preview['message_id'], photo=True)
                    self.api.call.assert_any_await('deleteMessage', chat_id=OWNER, message_id=warning_id)
                    self.assertEqual(self.store.repeat_warning_messages(OWNER), [])
                    with self.store.connect() as db:
                        db.execute('DELETE FROM posts')
                        db.execute('DELETE FROM post_products')
                    self.api.message.reset_mock()

    async def test_warning_is_private_to_its_owner(self):
        preview = self.new_preview()
        self.store.add_repeat_warning(OTHER, 9999)
        await self.click(f"p:{preview['id']}:edit", preview['message_id'], photo=True)
        self.assertEqual(self.store.repeat_warning_messages(OTHER), [9999])
        self.assertFalse(any(c.args[0] == 'deleteMessage' and c.kwargs['message_id'] == 9999
                             for c in self.api.call.await_args_list))

    async def test_v3_migration_recovers_existing_scheduled_photo_and_backup_restores(self):
        post = self.new_post()
        view = self.view(post, 'confirmation', {'media': True})
        with self.store.connect() as db:
            db.execute('DROP TABLE publication_cards')
            db.execute('DROP TABLE repeat_warnings')
            db.execute('PRAGMA user_version=3')
        upgraded = Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now)
        self.assertEqual(upgraded.get_post(post['id']), post)
        self.now = post['due'] + 1
        await Delivery(self.config, upgraded, self.api).send(post['id'])
        self.assertIn(view['message_id'], [card['message_id'] for card in upgraded.terminal_cards(post['id'], 'sent')])
        # Backups created by the upgraded image must remain restorable.
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as destination:
            copy = Store(Path(source) / 'bot.sqlite3')
            output = io.BytesIO()
            backup(Path(source), output)
            restore(Path(destination), io.BytesIO(output.getvalue()))
            with closing(sqlite3.connect(Path(destination) / 'bot.sqlite3')) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 6)

    async def test_telegram_recognizes_not_modified_without_retaining_description(self):
        description = 'Bad Request: message is not modified: private-content'
        transport = SimpleNamespace(request=lambda *a, **k: SimpleNamespace(status=400, body=json.dumps(
            {'ok': False, 'error_code': 400, 'description': description}).encode()))
        with self.assertRaises(TelegramError) as caught:
            await Telegram('fake', transport=transport).call('editMessageCaption')
        self.assertTrue(caught.exception.not_modified)
        self.assertNotIn('private-content', str(caught.exception))
