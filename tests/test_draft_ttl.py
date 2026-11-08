"""Draft TTL is configurable and measured from creation, including across restarts."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

from canal_ofertas_bot.config import Config, ConfigurationError
from canal_ofertas_bot.domain import Product
from canal_ofertas_bot.service import Service
from canal_ofertas_bot.storage import Store, Stale

OWNER, CHANNEL = 123456, '-1001234567890'
ENV = {'TELEGRAM_BOT_TOKEN': '123456:' + 'a'*35, 'TELEGRAM_OWNER_ID': str(OWNER), 'TELEGRAM_CHANNEL_ID': CHANNEL}


class TTLConfig(unittest.TestCase):
    def test_default_is_24_hours_and_environment_can_override(self):
        self.assertEqual(Config.from_env(ENV).integer('PREVIEW_TTL_SECONDS'), 86400)
        for duration in [60, 43200, 86400, 172800, 2592000]:
            with self.subTest(seconds=duration):
                self.assertEqual(Config.from_env({**ENV, 'PREVIEW_TTL_SECONDS': str(duration)}).integer('PREVIEW_TTL_SECONDS'), duration)
        for value in ['59', '2592001', 'un día']:
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    Config.from_env({**ENV, 'PREVIEW_TTL_SECONDS': value})


class DraftTTL(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1794052800.0
        self.path = Path(self.tmp.name) / 'bot.sqlite3'
        self.store = Store(self.path, now=lambda: self.now)
        self.product = Product('https://amzn.to/ficticio-0001', 'Producto', '17.99', '19.37')
        self.serial = 100
        self.config = Config.from_env({**ENV, 'DATA_DIR': self.tmp.name})
        self.api = SimpleNamespace(message=AsyncMock(side_effect=self.sent), photo=AsyncMock(side_effect=self.sent), call=AsyncMock(return_value=True))

    def sent(self, *args, **kwargs):
        self.serial += 1
        return {'message_id': self.serial, 'photo': [{'file_id': 'saved-photo'}]}

    def preview(self):
        self.store.replace_draft(OWNER, self.product, 1)
        self.store.update_draft(OWNER, self.product, b'source')
        return self.stage()

    def stage(self):
        fresh = self.store.stage_preview(OWNER, CHANNEL, b'image', 'Texto editado y guardado', [], product_data=self.product.to_dict())
        self.serial += 1
        self.store.activate(OWNER, fresh, self.serial, 'saved-photo')
        return self.store.preview(OWNER)

    async def test_default_is_available_until_exactly_24_hours_from_creation(self):
        first = self.preview()
        created = self.store.draft(OWNER)['created']
        self.assertEqual(self.store.ttl, 86400)
        self.assertEqual(first['expires'], created + 86400)
        self.now = created + 86399
        self.assertEqual(self.store.preview(OWNER)['id'], first['id'])
        self.assertIsNotNone(self.store.draft(OWNER))
        self.assertFalse(self.store.expired_messages((OWNER,)))
        self.now += 1
        with self.assertRaises(Stale): self.store.draft(OWNER)
        with self.assertRaises(Stale): self.store.preview(OWNER)
        self.assertTrue(self.store.expired_messages((OWNER,)))
        bot = Service(self.config, self.store, self.api)
        await bot.delivery.cleanup_expired()
        self.api.call.assert_any_await('deleteMessage', chat_id=OWNER, message_id=first['message_id'])

    async def test_editing_and_restart_do_not_renew_creation_time(self):
        self.preview()
        created = self.store.draft(OWNER)['created']
        self.now = created + 23*3600
        self.product.price = '18.00'
        self.store.update_draft(OWNER, self.product)
        current = self.stage()
        self.assertEqual(current['expires'], created + 86400)
        self.assertEqual(self.store.draft(OWNER)['created'], created)
        self.now += 1800
        self.store = Store(self.path, now=lambda: self.now)
        self.assertEqual(self.store.saved_preview(OWNER)['expires'], created + 86400)
        self.assertEqual(self.store.saved_preview(OWNER)['caption'], current['caption'])
        self.assertEqual(self.store.draft(OWNER)['created'], created)
        self.now = created + 86400
        with self.assertRaises(Stale): self.store.draft(OWNER)

    async def test_previa_moves_card_but_does_not_extend_24_hour_deadline(self):
        first = self.preview()
        created = self.store.draft(OWNER)['created']
        self.now = created + 86300
        bot = Service(self.config, self.store, self.api)
        await bot.handle({'message': {'message_id': 900, 'text': '/previa', 'from': {'id': OWNER}, 'chat': {'id': OWNER, 'type': 'private'}}})
        current = self.store.preview(OWNER)
        self.assertNotEqual(first['message_id'], current['message_id'])
        self.assertEqual(current['expires'], created + 86400)
        self.assertEqual(current['caption'], first['caption'])
        self.now += 100
        with self.assertRaises(Stale): self.store.preview(OWNER)

    async def test_larger_compose_ttl_updates_existing_draft_cards_and_warnings(self):
        self.store = Store(self.path, ttl=1800, now=lambda: self.now)
        first = self.preview()
        created = self.store.draft(OWNER)['created']
        self.store.add_repeat_warning(OWNER, 500)
        self.store.track_message(OWNER, 500)
        self.now += 900
        self.store = Store(self.path, ttl=86400, now=lambda: self.now)
        self.assertEqual(self.store.preview(OWNER)['expires'], created + 86400)
        with self.store.connect() as db:
            for message in [first['message_id'], 500]:
                self.assertEqual(db.execute('SELECT expires FROM ui_messages WHERE owner=? AND message_id=?', (OWNER, message)).fetchone()[0], created + 86400)
        self.now = created + 3600
        self.store.prune(OWNER, 0)
        self.assertEqual(self.store.preview(OWNER)['caption'], first['caption'])
        self.assertFalse(self.store.expired_messages((OWNER,)))

    async def test_shorter_override_also_counts_from_original_creation(self):
        self.preview()
        created = self.store.draft(OWNER)['created']
        self.now = created + 7200
        self.store = Store(self.path, ttl=3600, now=lambda: self.now)
        with self.assertRaises(Stale): self.store.draft(OWNER)
        self.assertTrue(self.store.expired_messages((OWNER,)))
        self.store.prune(OWNER, 0)
        self.store = Store(self.path, ttl=86400, now=lambda: self.now)
        with self.assertRaises(Stale): self.store.draft(OWNER)

    async def test_draft_expiry_does_not_cancel_confirmed_schedules(self):
        first = self.preview()
        post_id = self.store.consume(OWNER, first['id'], first['message_id'], CHANNEL, due=self.now+2*86400)
        post = self.store.get_post(post_id)
        other = self.preview()
        self.now += 86401
        bot = Service(self.config, self.store, self.api)
        await bot.delivery.tick()
        self.assertEqual(self.store.get_post(post_id), post)
        self.api.call.assert_any_await('deleteMessage', chat_id=OWNER, message_id=other['message_id'])
        self.assertFalse(any(c.args[0] == 'deleteMessage' and c.kwargs['message_id'] == first['message_id'] for c in self.api.call.await_args_list))
        self.api.photo.assert_not_awaited()

    async def test_restart_does_not_retime_existing_scheduled_editor(self):
        self.store = Store(self.path, ttl=1800, now=lambda: self.now)
        first = self.preview()
        post_id = self.store.consume(OWNER, first['id'], first['message_id'], CHANNEL, due=self.now+2*86400)
        post = self.store.get_post(post_id)
        token = self.store.add_scheduled_view(OWNER, post_id, post['revision'], 'editor', {'media': True}, (OWNER,))
        self.store.bind_scheduled_view(OWNER, token, 777)
        expires = self.store.scheduled_view(OWNER, token)['expires']
        self.preview()
        self.now += 900
        self.store = Store(self.path, ttl=86400, now=lambda: self.now)
        self.assertEqual(self.store.scheduled_view(OWNER, token)['expires'], expires)
        self.assertEqual(self.store.get_post(post_id), post)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT expires FROM ui_messages WHERE owner=? AND message_id=?', (OWNER, 777)).fetchone()[0], expires)
