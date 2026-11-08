"""Approved workflows exercised with real image rendering and no Telegram access."""
from io import BytesIO
import unittest
from unittest.mock import AsyncMock
from PIL import Image
import test_menus_scheduled as fixtures
from test_menus_scheduled import OWNER, OTHER, CHANNEL, URL
from canal_ofertas_bot.config import Config
from canal_ofertas_bot.domain import Product, compose_caption, units, validate_caption
from canal_ofertas_bot.editing import synchronize
from canal_ofertas_bot.footer import parse_footer, restore_footer
from canal_ofertas_bot.storage import Store, Stale
from canal_ofertas_bot import ui
from canal_ofertas_bot.post_ui import scheduled_card_text


class SignatureTests(unittest.TestCase):
    def test_unicode_link_and_visible_budget(self):
        footer = '👾 Oferta de [DanieloTech](https://t.me/danielotech)'
        text, entities = parse_footer(footer)
        self.assertEqual(text, '👾 Oferta de DanieloTech')
        self.assertEqual(entities[0]['offset'], units('👾 Oferta de '))
        p = Product(URL, 'Un título largo ' * 100, '14.99', '19.99', deals=['Oferta flash'])
        caption, entities = compose_caption(p, footer=footer)
        self.assertLessEqual(units(caption), 256)
        self.assertTrue(caption.endswith('\n\n' + text))
        self.assertEqual(entities[-1]['url'], 'https://t.me/danielotech')
        validate_caption(caption, entities, has_photo=True)

    def test_optional_mention_and_no_brand_by_default(self):
        self.assertEqual(parse_footer(''), ('', []))
        text, entities = parse_footer('🗣️ Visto en @danielotech')
        self.assertEqual(entities[0]['type'], 'mention')
        self.assertEqual(text.encode('utf-16-le')[entities[0]['offset']*2:(entities[0]['offset']+entities[0]['length'])*2].decode('utf-16-le'), '@danielotech')

    def test_plain_copy_restores_only_intact_signature(self):
        footer = '🗣️ Visto en [DanieloTech](https://t.me/danielotech)'
        self.assertTrue(restore_footer('Oferta\n\n🗣️ Visto en DanieloTech', [], footer))
        self.assertEqual(restore_footer('Oferta sin firma', [], footer), [])
        self.assertEqual(restore_footer('Otra 🗣️ Visto en DanieloTech', [], footer), [])

    def test_oversize_fixed_content_raises(self):
        p = Product('https://amzn.to/' + 'x' * 220, 'Producto', '14.99')
        with self.assertRaises(ValueError):
            compose_caption(p, footer='Una firma que ocupa demasiado espacio')

    def test_help_order_and_no_fixed_expiry(self):
        text, _ = ui.section('❓ - Ayuda', ui.HELP_BODY)
        self.assertLessEqual(units(text), 4096)
        first = [text.index(x) for x in ('Preparar oferta:', 'Borrador:', 'Programadas:', 'Usuarios:', 'Estado:', 'Ayuda:', 'Acerca de:', 'Cerrar:')]
        self.assertEqual(first, sorted(first))
        self.assertNotIn('30 minutos', text)
        self.assertTrue(text.endswith('Para consultar todos los comandos manuales usa /help.'))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.p = Product(URL, 'Producto', '80.00', '100.00', deals=['Oferta flash'])
        self.original = '📦 Producto\n\n🏷️ Oferta flash\n\n🔥 20 % de descuento\n\n👉 De 100,00 € a 80,00 €'

    def edit(self, text, choice=None):
        return synchronize(text, [], self.original, self.p, template=True, choice=choice)

    def test_prices_update_both_text_and_product(self):
        edit = self.edit(self.original.replace('80,00', '75,00'))
        self.assertIn('25 %', edit.caption)
        self.assertEqual(edit.product.price, '75.00')
        self.assertEqual(edit.product.discount, 25)

    def test_percentage_keeps_reference(self):
        edit = self.edit(self.original.replace('20 %', '30 %'))
        self.assertIn('70,00 €', edit.caption)
        self.assertEqual(edit.product.reference, '100.00')
        self.assertEqual(edit.product.price, '70.00')

    def test_conflict_and_three_resolutions(self):
        changed = self.original.replace('80,00', '85,00').replace('20 %', '30 %')
        self.assertEqual(self.edit(changed).conflict, (15, '30'))
        self.assertIn('15 %', self.edit(changed, 'prices').caption)
        self.assertIn('70,00 €', self.edit(changed, 'discount').caption)
        self.assertEqual(self.edit(changed, 'keep').caption, changed)
        self.assertEqual(self.edit(changed, 'keep').product.discount, 30)

    def test_custom_and_removed_label(self):
        edit = self.edit(self.original.replace('Oferta flash', 'Cupón: TECH'))
        self.assertEqual(edit.product.label, 'Cupón: TECH')
        self.assertEqual(edit.product.price, self.p.price)
        self.assertEqual(self.edit(self.original.replace('🏷️ Oferta flash\n\n', '')).product.label, '')

    def test_hidden_label_survives_other_field_edits(self):
        original = compose_caption(self.p, show_label=False)[0]
        changes = {
            'title': original.replace('📦 Producto', '📦 Producto editado'),
            'price': original.replace('80,00', '75,00'),
            'reference': original.replace('100,00', '120,00'),
            'discount': original.replace('20 %', '30 %'),
        }
        for field, caption in changes.items():
            with self.subTest(field=field):
                edited = synchronize(caption, [], original, self.p, template=True)
                self.assertEqual(edited.product.label, 'Oferta flash')
                self.assertIsNone(edited.product.custom_label)
                self.assertNotIn('🏷', edited.caption)

    def test_hidden_custom_label_survives_title_edit(self):
        self.p.custom_label = 'Cupón: TECH'
        original = compose_caption(self.p, show_label=False)[0]
        edited = synchronize(original.replace('📦 Producto', '📦 Producto editado'),
                             [], original, self.p, template=True)
        self.assertEqual(edited.product.custom_label, 'Cupón: TECH')
        self.assertNotIn('🏷', edited.caption)

    def test_adding_then_removing_label_does_not_restore_extracted_label(self):
        original = compose_caption(self.p, show_label=False)[0]
        added = synchronize(original + '\n\n🏷️ Oferta del día', [], original, self.p, template=True)
        self.assertEqual(added.product.label, 'Oferta del día')
        removed = synchronize(original, [], added.caption, added.product, template=True)
        self.assertEqual(removed.product.label, '')
        edited = synchronize(original.replace('80,00', '75,00'), [], original, removed.product, template=True)
        self.assertEqual(edited.product.label, '')

    def test_explicit_empty_label_clears_hidden_promotion(self):
        original = compose_caption(self.p, show_label=False)[0]
        edited = synchronize(original + '\n\n🏷️ ', [], original, self.p, template=True)
        self.assertEqual(edited.product.label, '')

    def test_full_image_text_keeps_manual_values(self):
        changed = self.original.replace('20 %', '30 %')
        edit = synchronize(changed, [], self.original, self.p, template=False)
        self.assertEqual(edit.caption, changed)
        self.assertEqual(edit.product.to_dict(), self.p.to_dict())

    def test_no_reference_does_not_invent_one(self):
        with self.assertRaisesRegex(ValueError, 'precio anterior'):
            synchronize('🔥 30 % de descuento\n👉 80,00 €', [], '🔥 20 % de descuento\n👉 80,00 €', Product(URL, 'P', '80.00'), template=True)


class Workflows(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ScheduledTests.setUp
    sent = fixtures.ScheduledTests.sent
    click = fixtures.ScheduledTests.click
    q = fixtures.ScheduledTests.q
    latest_view = fixtures.ScheduledTests.latest_view
    text = fixtures.ScheduledTests.text
    new_preview = fixtures.ScheduledTests.new_preview
    new_post = fixtures.ScheduledTests.new_post
    view = fixtures.ScheduledTests.view

    def photo(self):
        output = BytesIO()
        Image.new('RGB', (120, 100), 'blue').save(output, 'JPEG')
        return output.getvalue()

    async def incoming_photo(self):
        self.serial += 1
        self.api.download_photo = AsyncMock(return_value=self.photo())
        await self.bot.handle({'message': {'message_id': self.serial, 'photo': [{'file_id': 'incoming'}],
                               'from': {'id': OWNER}, 'chat': {'id': OWNER, 'type': 'private'}}})

    async def generated(self):
        self.product.price, self.product.reference = '80.00', '100.00'
        self.store.replace_draft(OWNER, self.product, 1)
        self.store.update_draft(OWNER, self.product, self.photo())
        await self.bot.make_preview()
        return self.store.preview(OWNER)

    async def edit_new(self, preview):
        await self.click(f"p:{preview['id']}:edit", preview['message_id'], photo=bool(preview['photo']))
        return self.store.editor(OWNER)

    async def test_template_prices_regenerate_real_image(self):
        preview = await self.generated()
        await self.edit_new(preview)
        await self.text(preview['caption'].replace('80,00', '75,00'))
        edited = self.store.preview(OWNER)
        self.assertIn('25 %', edited['caption'])
        self.assertNotEqual(edited['photo'], preview['photo'])
        self.assertEqual(edited['product_data']['price'], '75.00')

    def assert_promotion_badge(self, photo, visible):
        with Image.open(BytesIO(photo)) as image:
            red, green, blue = image.convert('RGB').getpixel((48, 126))
        if visible:
            self.assertGreater(green, red + 30)
            self.assertGreater(green, blue + 10)
        else:
            self.assertGreater(min(red, green, blue), 240)

    async def test_hidden_caption_price_edit_keeps_image_promotion(self):
        self.config.values['SHOW_PROMOTION_IN_CAPTION'] = 'false'
        preview = await self.generated()
        self.assertNotIn('🏷', preview['caption'])
        self.assert_promotion_badge(preview['photo'], True)
        await self.edit_new(preview)
        await self.text(preview['caption'].replace('80,00', '75,00'))
        edited = self.store.preview(OWNER)
        self.assertEqual(edited['product_data']['price'], '75.00')
        self.assertEqual(Product(**edited['product_data']).label, 'Oferta flash')
        self.assertNotIn('🏷', edited['caption'])
        self.assertNotEqual(edited['photo'], preview['photo'])
        self.assert_promotion_badge(edited['photo'], True)

    async def test_added_hidden_label_can_be_removed_from_image(self):
        self.config.values['SHOW_PROMOTION_IN_CAPTION'] = 'false'
        preview = await self.generated()
        await self.edit_new(preview)
        await self.text(preview['caption'] + '\n\n🏷️ Oferta del día')
        added = self.store.preview(OWNER)
        self.assertEqual(Product(**added['product_data']).label, 'Oferta del día')
        self.assert_promotion_badge(added['photo'], True)
        await self.edit_new(added)
        await self.text(added['caption'].replace('\n\n🏷️ Oferta del día', ''))
        removed = self.store.preview(OWNER)
        self.assertEqual(Product(**removed['product_data']).label, '')
        self.assert_promotion_badge(removed['photo'], False)

    async def test_added_caption_label_respects_disabled_image_promotion(self):
        self.config.values['SHOW_PROMOTION_IN_CAPTION'] = 'false'
        self.config.values['SHOW_PROMOTION_IN_IMAGE'] = 'false'
        preview = await self.generated()
        self.assert_promotion_badge(preview['photo'], False)
        await self.edit_new(preview)
        await self.text(preview['caption'] + '\n\n🏷️ Oferta del día')
        edited = self.store.preview(OWNER)
        self.assertIn('🏷️ Oferta del día', edited['caption'])
        self.assertEqual(Product(**edited['product_data']).label, 'Oferta del día')
        self.assert_promotion_badge(edited['photo'], False)

    async def test_scheduled_hidden_promotion_survives_edit_and_save(self):
        self.config.values['SHOW_PROMOTION_IN_CAPTION'] = 'false'
        preview = await self.generated()
        post_id = self.store.consume(OWNER, preview['id'], preview['message_id'], CHANNEL, due=self.now+3600)
        post = self.store.get_post(post_id)
        await self.q(self.view(post, 'preview', {'media': True}), 'edit')
        await self.text(post['caption'].replace('80,00', '75,00'))
        review = self.latest_view('review')
        self.assertEqual(self.store.get_post(post_id), post)
        self.assert_promotion_badge(review['state']['photo'], True)
        await self.q(review, 'save')
        saved = self.store.get_post(post_id)
        self.assertEqual(saved['due'], post['due'])
        self.assertEqual(saved['product_data']['price'], '75.00')
        self.assertEqual(Product(**saved['product_data']).label, 'Oferta flash')
        self.assertNotIn('🏷', saved['caption'])
        self.assert_promotion_badge(saved['photo'], True)

    async def test_remove_then_add_image_and_publish_text_only(self):
        preview = await self.generated()
        editor = await self.edit_new(preview)
        await self.click(f"e:{editor['preview_id']}:remove", editor['prompt'])
        plain = self.store.preview(OWNER)
        self.assertFalse(plain['photo'])
        self.assertFalse(plain['photo_id'])
        await self.click(f"p:{plain['id']}:publish", plain['message_id'])
        sent = [c for c in self.api.message.await_args_list if c.args[0] == CHANNEL]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].args[1], plain['caption'])
        self.assertFalse(any(c.args[0] == CHANNEL for c in self.api.photo.await_args_list))

    async def test_replace_template_with_full_image(self):
        preview = await self.generated()
        editor = await self.edit_new(preview)
        await self.click(f"e:{editor['preview_id']}:replace", editor['prompt'])
        await self.incoming_photo()
        current = self.store.preview(OWNER)
        self.assertFalse(current['template'])
        self.assertEqual(current['caption'], preview['caption'])
        self.assertTrue(current['photo'])

    async def test_implicit_photo_keeps_template(self):
        preview = await self.generated()
        await self.edit_new(preview)
        await self.incoming_photo()
        self.assertTrue(self.store.preview(OWNER)['template'])

    async def test_free_text_and_changed_link(self):
        preview = self.new_preview()
        await self.edit_new(preview)
        await self.text('Mi texto libre, sin enlace ni precios.')
        self.assertEqual(self.store.preview(OWNER)['caption'], 'Mi texto libre, sin enlace ni precios.')

    async def test_image_only_scheduled_and_delivery(self):
        preview = self.new_preview()
        with self.store.connect() as db:
            db.execute("UPDATE previews SET caption='',entities='[]' WHERE id=?", (preview['id'],))
        post_id = self.store.consume(OWNER, preview['id'], preview['message_id'], CHANNEL, due=self.now+120)
        self.now += 121
        await self.bot.delivery.tick()
        self.api.photo.assert_awaited_once_with(CHANNEL, 'foto-ficticia', '', [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM receipts WHERE id=?', (post_id,)).fetchone()[0], 'sent')

    async def test_scheduled_remove_review_close_restores_same_schedule(self):
        post = self.new_post()
        view = self.view(post, 'preview', {'media': True})
        await self.q(view, 'edit')
        editor = self.latest_view('editor')
        await self.q(editor, 'remove')
        review = self.latest_view('review')
        self.assertFalse(review['state']['photo'])
        self.assertEqual(self.store.get_post(post['id']), post)
        await self.q(review, 'close')
        self.assertEqual(self.store.get_post(post['id']), post)
        self.assertTrue(self.latest_view('preview')['state']['media'])

    async def test_save_text_only_keeps_due_and_delivers_once(self):
        post = self.new_post()
        await self.q(self.view(post, 'preview', {'media': True}), 'edit')
        await self.q(self.latest_view('editor'), 'remove')
        await self.q(self.latest_view('review'), 'save')
        current = self.store.get_post(post['id'])
        self.assertEqual(current['due'], post['due'])
        self.assertFalse(current['photo'] or current['photo_id'])
        self.now = current['due'] + 1
        await self.bot.delivery.tick()
        await self.bot.delivery.tick()
        self.assertEqual(len([c for c in self.api.message.await_args_list if c.args[0] == CHANNEL]), 1)

    async def test_expiry_deletes_messages_after_restart_and_keeps_schedule(self):
        scheduled = self.new_post()
        preview = await self.generated()
        editor = await self.edit_new(preview)
        visible = {preview['message_id'], editor['prompt']}
        self.now += self.store.ttl + 1
        restarted = Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now)
        self.bot.delivery.store = restarted
        await self.bot.delivery.tick()
        removed = {c.kwargs['message_id'] for c in self.api.call.await_args_list if c.args[0] == 'deleteMessage'}
        self.assertTrue(visible <= removed)
        self.assertEqual(restarted.get_post(scheduled['id'])['status'], 'pending')
        with self.assertRaises(Stale): restarted.draft(OWNER)

    async def test_expired_reprogram_disappears_without_cancelling_post(self):
        post = self.new_post()
        await self.q(self.view(post, 'preview', {'media': True}), 'schedule')
        picker = self.latest_view('picker')
        self.now += self.store.ttl + 1
        await self.bot.delivery.tick()
        self.api.call.assert_any_await('deleteMessage', chat_id=OWNER, message_id=picker['message_id'])
        self.assertEqual(self.store.get_post(post['id'])['due'], post['due'])
        self.assertEqual(self.store.get_post(post['id'])['status'], 'pending')

    async def test_closed_draft_recovers_original_manual_text(self):
        preview = self.new_preview()
        await self.edit_new(preview)
        await self.text('Texto personalizado sin enlace')
        current = self.store.preview(OWNER)
        await self.click(f"p:{current['id']}:close", current['message_id'])
        await self.bot.show_draft()
        restored = self.store.preview(OWNER)
        self.assertEqual(restored['caption'], 'Texto personalizado sin enlace')
        await self.click(f"d:{restored['id']}:open", restored['message_id'], photo=True)
        self.assertEqual(self.store.preview(OWNER)['caption'], 'Texto personalizado sin enlace')

    async def test_other_user_cannot_modify_foreign_draft(self):
        preview = self.new_preview()
        await self.click(f"p:{preview['id']}:discard", preview['message_id'], OTHER)
        self.assertEqual(self.store.preview(OWNER)['id'], preview['id'])

    async def test_empty_draft_cannot_be_published(self):
        preview = self.new_preview()
        with self.store.connect() as db:
            db.execute("UPDATE previews SET caption='',photo=x'',photo_id='' WHERE id=?", (preview['id'],))
        with self.assertRaises(ValueError):
            self.store.consume(OWNER, preview['id'], preview['message_id'], CHANNEL)

    async def test_cancellation_of_programmed_preview_updates_card(self):
        post = self.new_post()
        await self.q(self.view(post, 'preview', {'media': True}), 'cancel')
        self.assertEqual(self.store.get_post(post['id'])['status'], 'cancelled')
        self.assertFalse(self.store.expired_messages((OWNER,)))

    async def test_manual_command_removes_only_caption(self):
        preview = await self.generated()
        await self.text('/editar texto ninguno')
        current = self.store.preview(OWNER)
        self.assertEqual(current['caption'], '')
        self.assertEqual(current['photo'], preview['photo'])
        self.assertEqual(current['product_data'], preview['product_data'])

    async def test_scheduled_command_image_only_waits_for_save(self):
        post = self.new_post()
        await self.q(self.view(post, 'preview', {'media': True}), 'edit')
        await self.text('/editar texto ninguno')
        review = self.latest_view('review')
        self.assertEqual(review['state']['caption'], '')
        self.assertEqual(self.store.get_post(post['id']), post)
        await self.q(review, 'save')
        self.assertEqual(self.store.get_post(post['id'])['caption'], '')

    async def test_deletion_failure_retries_after_restart(self):
        from canal_ofertas_bot.network import NetworkError
        preview = await self.generated()
        self.now += self.store.ttl + 1
        self.api.call.side_effect = NetworkError('temporal')
        await self.bot.delivery.cleanup_expired()
        self.assertTrue(self.store.expired_messages((OWNER,)))
        self.api.call.side_effect = None
        await self.bot.delivery.cleanup_expired()
        self.assertFalse(self.store.expired_messages((OWNER,)))
