"""Discount visibility follows edited text independently of price calculations."""
from io import BytesIO
import unittest
from PIL import Image

import test_menus_scheduled as fixtures
import test_approved_changes as workflows
from canal_ofertas_bot.domain import Product, compose_caption
from canal_ofertas_bot.editing import synchronize
from canal_ofertas_bot.service import Service
from canal_ofertas_bot.storage import Store

OWNER, CHANNEL = fixtures.OWNER, fixtures.CHANNEL


class VisibilityRules(unittest.TestCase):
    def setUp(self):
        self.product = Product(fixtures.URL, 'Gafas', '17.99', '19.37')
        self.original = compose_caption(self.product)[0]
        self.without = self.original.replace('🔥 7 % de descuento\n\n', '')

    def test_deleting_discount_line_keeps_both_prices_and_discount_calculation(self):
        edited = synchronize(self.without, [], self.original, self.product, template=True)
        self.assertFalse(edited.product.show_discount)
        self.assertEqual((edited.product.price, edited.product.reference), ('17.99', '19.37'))
        self.assertEqual(edited.product.discount, 7)
        self.assertNotIn('de descuento', edited.caption)
        generated = compose_caption(edited.product)[0]
        self.assertNotIn('de descuento', generated)
        self.assertIn('De 19,37 € a 17,99 €', generated)

    def test_price_changes_do_not_restore_hidden_discount(self):
        hidden = synchronize(self.without, [], self.original, self.product, template=True)
        changed = synchronize(hidden.caption.replace('17,99', '16,99'), [], hidden.caption, hidden.product, template=True)
        self.assertFalse(changed.product.show_discount)
        self.assertEqual(changed.product.price, '16.99')
        self.assertEqual(changed.product.reference, '19.37')
        self.assertNotIn('de descuento', changed.caption)

    def test_restoring_same_rounded_percentage_preserves_prices(self):
        hidden = synchronize(self.without, [], self.original, self.product, template=True)
        restored = synchronize(self.original, [], hidden.caption, hidden.product, template=True)
        self.assertTrue(restored.product.show_discount)
        self.assertEqual((restored.product.price, restored.product.reference), ('17.99', '19.37'))
        self.assertEqual(restored.product.discount, 7)

    def test_adding_a_different_percentage_still_recalculates_price(self):
        hidden = synchronize(self.without, [], self.original, self.product, template=True)
        restored = synchronize(self.original.replace('7 %', '10 %'), [], hidden.caption, hidden.product, template=True)
        self.assertTrue(restored.product.show_discount)
        self.assertEqual(restored.product.price, '17.43')
        self.assertEqual(restored.product.discount, 10)

    def test_custom_image_does_not_change_its_product_or_visibility(self):
        edited = synchronize(self.without, [], self.original, self.product, template=False)
        self.assertEqual(edited.product.to_dict(), self.product.to_dict())

    def test_old_product_records_show_discount_by_default(self):
        old = self.product.to_dict()
        old.pop('show_discount', None)
        self.assertTrue(Product(**old).show_discount)


class VisibilityWorkflows(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ScheduledTests.setUp
    sent = fixtures.ScheduledTests.sent
    click = fixtures.ScheduledTests.click
    q = fixtures.ScheduledTests.q
    view = fixtures.ScheduledTests.view
    text = fixtures.ScheduledTests.text
    latest_view = fixtures.ScheduledTests.latest_view
    photo = workflows.Workflows.photo
    edit_new = workflows.Workflows.edit_new

    async def generated(self):
        self.product.price, self.product.reference = '17.99', '19.37'
        self.store.replace_draft(OWNER, self.product, 1)
        self.store.update_draft(OWNER, self.product, self.photo())
        await self.bot.make_preview()
        return self.store.preview(OWNER)

    def assert_badge(self, photo, visible):
        with Image.open(BytesIO(photo)) as image:
            red, green, blue = image.convert('RGB').getpixel((1000, 40))
        if visible:
            self.assertGreater(red, 180)
            self.assertLess(green, 160)
            self.assertLess(blue, 160)
        else:
            self.assertGreater(min(red, green, blue), 240)

    async def test_removing_percentage_hides_badge_in_real_generated_image(self):
        initial = await self.generated()
        self.assert_badge(initial['photo'], True)
        await self.edit_new(initial)
        await self.text(initial['caption'].replace('🔥 7 % de descuento\n\n', ''))
        hidden = self.store.preview(OWNER)
        self.assert_badge(hidden['photo'], False)
        self.assertEqual(hidden['product_data']['price'], '17.99')
        self.assertEqual(hidden['product_data']['reference'], '19.37')
        self.assertFalse(hidden['product_data']['show_discount'])
        self.assertNotIn('de descuento', hidden['caption'])
        self.assertNotEqual(hidden['photo'], initial['photo'])

    async def test_hidden_badge_survives_restart_and_previa_then_can_be_restored(self):
        initial = await self.generated()
        await self.edit_new(initial)
        await self.text(initial['caption'].replace('🔥 7 % de descuento\n\n', ''))
        saved = self.store.preview(OWNER)
        self.store = Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now)
        self.bot = Service(self.config, self.store, self.api)
        await self.text('/previa')
        current = self.store.preview(OWNER)
        self.assertFalse(current['product_data']['show_discount'])
        self.assertEqual(current['caption'], saved['caption'])
        self.assertEqual(current['photo'], saved['photo'])
        self.assert_badge(current['photo'], False)
        await self.edit_new(current)
        await self.text(initial['caption'])
        restored = self.store.preview(OWNER)
        self.assert_badge(restored['photo'], True)
        self.assertEqual(restored['product_data']['price'], '17.99')
        self.assertEqual(restored['product_data']['reference'], '19.37')

    async def test_scheduled_edit_hides_badge_only_after_save(self):
        initial = await self.generated()
        post_id = self.store.consume(OWNER, initial['id'], initial['message_id'], CHANNEL, due=self.now+3600)
        original = self.store.get_post(post_id)
        await self.q(self.view(original, 'preview', {'media': True}), 'edit')
        await self.text(initial['caption'].replace('🔥 7 % de descuento\n\n', ''))
        review = self.latest_view('review')
        self.assert_badge(review['state']['photo'], False)
        self.assertEqual(self.store.get_post(post_id), original)
        await self.q(review, 'save')
        saved = self.store.get_post(post_id)
        self.assertFalse(saved['product_data']['show_discount'])
        self.assert_badge(saved['photo'], False)
        self.assertEqual(saved['due'], original['due'])
        self.now = saved['due'] + 1
        self.api.photo.reset_mock()
        await self.bot.delivery.tick()
        sent = self.api.photo.await_args
        self.assertEqual(sent.args[0], CHANNEL)
        self.assertEqual(sent.args[1], saved['photo_id'] or saved['photo'])
        self.assertNotIn('de descuento', sent.args[2])
