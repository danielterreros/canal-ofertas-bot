"""Run from the project directory: python -m unittest discover -s tests -v."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from canal_ofertas_bot.config import Config
from canal_ofertas_bot.domain import Product, compose_caption, restore_price_format, units, validate_caption
from canal_ofertas_bot.service import Service
from canal_ofertas_bot.storage import Stale, Store


URL = "https://amzn.to/ficticio-0001"
TITLE = ("COMFEE' Deshumidificador de Humedad interior 20L / 2-EN-1 Purificador Eléctrico "
         "Silencioso con Ionizador, App/WIFI, 4 modos, Bajo Consumo con Depósito Extraible "
         "| Easy Dry 20 hasta 52 m² / 150 m³")
OFFER_END = "\n\n🏷️ Oferta flash\n\n🔥 8 % de descuento\n\n👉 De 195,10 € a 179,99 €"
OWNER = 123456
CHANNEL = "-1001234567890"


def product(**overrides):
    values = dict(original_url=URL, title=TITLE, price="179.99", reference="195.10",
                  deals=["Oferta flash"], image_url="https://m.media-amazon.com/images/I/ficticio.jpg")
    values.update(overrides)
    return Product(**values)



class PriceFormatTests(unittest.TestCase):
    def test_edited_prices_and_emoji_offsets(self):
        text = URL + "\n\n📦 🛍️ Título modificado\n\n👉 De 1.441,55 € a 999,99 €\n"
        entities = restore_price_format(text, [])
        encoded = text.encode("utf-16-le")
        values = {e["type"]: encoded[e["offset"] * 2:(e["offset"] + e["length"]) * 2].decode("utf-16-le")
                  for e in entities}
        self.assertEqual(values, {"strikethrough": "1.441,55 €", "bold": "999,99 €"})
        validate_caption(text, entities)

    def test_current_price_without_reference(self):
        text = URL + "\n\n👉 299,99 €"
        self.assertEqual(restore_price_format(text, []), [
            {"type": "bold", "offset": units(text[:-8]), "length": 8}])

    def test_manual_format_is_preserved_and_not_duplicated(self):
        text = URL + "\n\n👉 De 441,55 € a 299,99 €"
        manual = [{"type": "italic", "offset": units(text[:-8]), "length": 8}]
        saved = [dict(e) for e in manual]
        result = restore_price_format(text, manual)
        self.assertEqual(manual, saved)
        self.assertIn(manual[0], result)
        self.assertEqual([e["type"] for e in result], ["strikethrough", "italic"])
        self.assertEqual(restore_price_format(text, result), result)

    def test_unrecognized_or_nonfinal_price_line_is_unchanged(self):
        for text in ("Precio 299,99 €", "👉 299,9 €"):
            with self.subTest(text=text):
                self.assertEqual(restore_price_format(text, []), [])


class CaptionTests(unittest.TestCase):
    def assert_prices(self, caption, entities, expected):
        encoded = caption.encode("utf-16-le")
        formatted = {entity["type"]: encoded[entity["offset"] * 2:
                                            (entity["offset"] + entity["length"]) * 2].decode("utf-16-le")
                     for entity in entities}
        self.assertEqual(formatted, expected)

    def test_comfee_offer_shortens_only_title_at_a_word_boundary(self):
        p = product()
        original = p.to_dict()
        caption, entities = compose_caption(p)
        shortened = ("COMFEE' Deshumidificador de Humedad interior 20L / 2-EN-1 Purificador Eléctrico "
                     "Silencioso con Ionizador, App/WIFI, 4 modos, Bajo Consumo con Depósito…")
        self.assertEqual(caption, URL + "\n\n📦 " + shortened + OFFER_END)
        self.assertLessEqual(units(caption), 256)
        self.assertEqual(p.to_dict(), original)
        self.assert_prices(caption, entities, {"strikethrough": "195,10 €", "bold": "179,99 €"})
        validate_caption(caption, entities)

    def test_short_title_is_preserved_exactly(self):
        title = "Café & <producto> – 20 m²"
        caption, _ = compose_caption(product(title=title))
        self.assertEqual(caption, URL + "\n\n📦 " + title + OFFER_END)

    def test_title_that_fits_exactly_is_not_shortened(self):
        prefix, suffix = URL + "\n\n📦 ", "\n\n👉 179,99 €"
        title = "x" * (256 - units(prefix + suffix))
        p = product(title=title, reference="", deals=[])
        caption, entities = compose_caption(p)
        self.assertEqual(caption, prefix + title + suffix)
        self.assertEqual(units(caption), 256)
        self.assert_prices(caption, entities, {"bold": "179,99 €"})
        p.title += "x"
        shortened, _ = compose_caption(p)
        self.assertEqual(shortened, prefix + title[:-1] + "…" + suffix)
        self.assertEqual(units(shortened), 256)

    def test_very_long_title_without_spaces_still_produces_a_copyable_offer(self):
        caption, entities = compose_caption(product(title="x" * 1100))
        self.assertEqual(units(caption), 256)
        self.assertTrue(caption.startswith(URL + "\n\n📦 xxx"))
        self.assertTrue(caption.endswith("…" + OFFER_END))
        validate_caption(caption, entities)

    def test_astral_characters_do_not_overflow_or_split_unicode(self):
        for title in ("💶" * 300, "Cámara 📷 con batería 🔋 y pantalla táctil " * 30):
            with self.subTest(title=title[:40]):
                caption, entities = compose_caption(product(title=title, emoji="🛍️"))
                self.assertLessEqual(units(caption), 256)
                self.assertEqual(caption.encode("utf-16-le").decode("utf-16-le"), caption)
                self.assertTrue(caption.endswith("…" + OFFER_END))
                self.assert_prices(caption, entities, {"strikethrough": "195,10 €", "bold": "179,99 €"})

    def test_label_setting_changes_title_budget_without_changing_prices(self):
        shown, shown_entities = compose_caption(product())
        hidden, hidden_entities = compose_caption(product(), show_label=False)
        self.assertIn("🏷️ Oferta flash", shown)
        self.assertNotIn("🏷️", hidden)
        self.assertGreater(len(hidden.split("\n\n")[1]), len(shown.split("\n\n")[1]))
        for caption, entities in ((shown, shown_entities), (hidden, hidden_entities)):
            self.assertLessEqual(units(caption), 256)
            self.assert_prices(caption, entities, {"strikethrough": "195,10 €", "bold": "179,99 €"})

    def test_no_reference_does_not_invent_a_discount_or_previous_price(self):
        caption, entities = compose_caption(product(title=TITLE * 10, reference=""))
        self.assertLessEqual(units(caption), 256)
        self.assertTrue(caption.endswith("\n\n🏷️ Oferta flash\n\n👉 179,99 €"))
        self.assertNotIn("descuento", caption)
        self.assertNotIn("195,10", caption)
        self.assert_prices(caption, entities, {"bold": "179,99 €"})

    def test_long_affiliate_link_is_preserved_in_full(self):
        url = "https://www.amazon.es/dp/B0DEMO0001?tag=afiliacion-ficticia-21&x=a%2Fb"
        p = product(original_url=url)
        caption, entities = compose_caption(p)
        self.assertEqual(caption.splitlines()[0], url)
        self.assertLessEqual(units(caption), 256)
        self.assertTrue(caption.endswith(OFFER_END))
        self.assertEqual(p.title, TITLE)
        validate_caption(caption, entities)

    def test_campaign_label_is_preserved(self):
        caption, _ = compose_caption(product(deals=[], campaigns=["Ofertas de última hora para regalos"]))
        self.assertIn("\n\n🏷️ Ofertas de última hora para regalos\n\n", caption)
        self.assertTrue(caption.endswith("🔥 8 % de descuento\n\n👉 De 195,10 € a 179,99 €"))
        self.assertLessEqual(units(caption), 256)

    def test_fixed_content_that_does_not_fit_is_rejected_without_mutating_product(self):
        p = product(original_url="https://www.amazon.es/dp/B0DEMO0001?tag=" + "x" * 256)
        original = p.to_dict()
        with self.assertRaisesRegex(ValueError, "enlace corto oficial"):
            compose_caption(p)
        self.assertEqual(p.to_dict(), original)

    def test_minimum_title_budget_includes_ellipsis(self):
        suffix = "\n\n📦 \n\n👉 179,99 €"
        url = "https://amzn.to/"
        url += "x" * (255 - units(url + suffix))
        p = product(original_url=url, reference="", deals=[])
        caption, entities = compose_caption(p)
        self.assertEqual(caption, url + "\n\n📦 …\n\n👉 179,99 €")
        self.assertEqual(units(caption), 256)
        validate_caption(caption, entities)
        p.original_url += "x"
        with self.assertRaisesRegex(ValueError, "enlace corto oficial"):
            compose_caption(p)

    def test_manual_caption_limit_counts_emojis_and_newlines(self):
        text = URL + "\n" + "x" * (1024 - units(URL + "\n"))
        validate_caption(text, [], has_photo=True)
        for invalid in (text + "x", text[:-1] + "💶"):
            with self.subTest(text=invalid[-4:]), self.assertRaisesRegex(ValueError, r"1\.024"):
                validate_caption(invalid, [], has_photo=True)

    def test_manual_captions_larger_than_copy_button_limit_are_valid(self):
        for length in (256, 257, 500, 1024):
            with self.subTest(length=length):
                text = URL + "\n" + "x" * (length - units(URL + "\n"))
                validate_caption(text, [], has_photo=True)

    def test_validation_allows_free_links_and_preserves_entity_boundaries(self):
        text = URL + '\nProducto'
        validate_caption(text.replace(URL, 'https://example.com/otro'), [])
        validate_caption('Texto sin enlace', [])
        with self.assertRaises(ValueError):
            validate_caption('📦 Texto', [{'type': 'bold', 'offset': 1, 'length': 1}])


class CopyFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc).timestamp()
        self.serial = 100
        self.config = Config.from_env({
            "TELEGRAM_BOT_TOKEN": "123456:" + "a" * 35,
            "TELEGRAM_OWNER_ID": str(OWNER), "TELEGRAM_CHANNEL_ID": CHANNEL,
            "DATA_DIR": temporary.name,
        })
        self.store = Store(Path(temporary.name) / "bot.sqlite3", now=lambda: self.now)
        self.api = SimpleNamespace(message=AsyncMock(side_effect=self.sent),
                                   photo=AsyncMock(side_effect=self.sent), call=AsyncMock(return_value=True))
        self.amazon = SimpleNamespace(extract=AsyncMock(return_value=product()),
                                      download=AsyncMock(return_value=b"foto ficticia"))
        self.bot = Service(self.config, self.store, self.api, self.amazon)
        rendering = patch("canal_ofertas_bot.service.render_card", return_value=b"tarjeta ficticia")
        rendering.start()
        editor_rendering = patch("canal_ofertas_bot.editor_ui.render_card", return_value=b"tarjeta editada ficticia")
        editor_rendering.start()
        self.addCleanup(editor_rendering.stop)
        self.addCleanup(rendering.stop)

    def next_id(self):
        self.serial += 1
        return self.serial

    def sent(self, *args, **kwargs):
        return {"message_id": self.next_id(), "photo": [{"file_id": "foto-telegram-ficticia"}]}

    async def text(self, text, entities=None):
        await self.bot.handle({"message": {"message_id": self.next_id(), "text": text,
                              "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER},
                              "entities": entities or []}})

    async def action(self, action, preview, prefix="p"):
        await self.bot.handle({"callback_query": {"id": str(self.next_id()), "from": {"id": OWNER},
                              "data": f"{prefix}:{preview['id']}:{action}",
                              "message": {"message_id": preview["message_id"],
                                          "chat": {"id": OWNER, "type": "private"}}}})

    async def prepare(self):
        await self.text(URL)
        return self.store.preview(OWNER)

    async def expanded_preview(self, length=500):
        original = await self.prepare()
        await self.action("edit", original)
        edited = original["caption"] + "\n\n" + "x" * (length - units(original["caption"]) - 2)
        await self.text(edited, original["entities"])
        return original, edited, self.store.preview(OWNER)

    def assert_single_copy(self, caption):
        _, instructions, keyboard, _ = self.api.message.call_args.args
        copies = [b for row in keyboard for b in row if 'copy_text' in b]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0]['copy_text']['text'], caption)
        self.assertEqual(keyboard[-1][0]['text'], '❌ - Cerrar')
        self.assertLessEqual(units(caption), 256)
        self.assertNotIn('varias partes', instructions)

    def assert_native_copy(self):
        _, instructions, keyboard, _ = self.api.message.call_args.args
        self.assertFalse(any('copy_text' in b for row in keyboard for b in row))
        self.assertEqual(keyboard[-1][0]['text'], '❌ - Cerrar')
        self.assertIn('Selecciona el texto de la vista previa', instructions)
        self.assertNotIn('256', instructions)

    async def test_link_extracts_full_title_and_opens_one_copy_button(self):
        preview = await self.prepare()
        self.amazon.extract.assert_awaited_once_with(URL)
        self.assertEqual(self.store.draft(OWNER)["data"]["title"], TITLE)
        self.assertTrue(preview["caption"].endswith(OFFER_END))
        self.assertEqual(self.api.photo.call_args.args[0], OWNER)
        await self.action("edit", preview)
        self.assert_single_copy(preview["caption"])

    async def test_plain_text_edit_restores_price_styles(self):
        original = await self.prepare()
        await self.action("edit", original)
        edited = URL + "\n\n📦 🛍️ Título cambiado\n\n👉 De 441,55 € a 299,99 €"
        await self.text(edited)
        current = self.store.preview(OWNER)
        self.assertEqual(current["caption"], edited)
        self.assertEqual(current["entities"], restore_price_format(edited, []))
        self.assertEqual(len(current["entities"]), 2)
        updates = [c for c in self.api.call.await_args_list if c.args[0] == 'editMessageMedia']
        self.assertEqual(updates[-1].kwargs['media']['caption_entities'], current['entities'])

    async def test_formatted_edit_creates_a_new_copyable_preview(self):
        original = await self.prepare()
        await self.action("edit", original)
        edited = URL + "\n\n📦 Café & <literal>\n\n👉 179,99 €"
        entities = [{"type": "bold", "offset": units(edited[:edited.index("179,99 €")]), "length": 8}]
        await self.text(edited, entities)
        current = self.store.preview(OWNER)
        self.assertNotEqual(current["id"], original["id"])
        self.assertEqual(current["caption"], edited)
        self.assertEqual(current["entities"], entities)
        self.assertTrue(current["photo"] or current["photo_id"])
        self.assertIsNone(self.store.editor(OWNER))
        await self.action("edit", current)
        self.assert_single_copy(edited)

    async def test_oversize_manual_edit_leaves_editor_available_for_a_retry(self):
        original = await self.prepare()
        await self.action("edit", original)
        editor = self.store.editor(OWNER)
        invalid = original["caption"] + "x" * (1025 - units(original["caption"]))
        await self.text(invalid)
        self.assertEqual(self.api.photo.await_count, 1)
        self.assertEqual(self.store.editor(OWNER), editor)
        self.assertEqual(self.store.preview(OWNER, editing=True)["caption"], original["caption"])
        self.assertIn("1.024", self.api.message.call_args.args[1])
        await self.text(original["caption"])
        self.assertEqual(self.store.preview(OWNER)["caption"], original["caption"])
        self.assertIsNone(self.store.editor(OWNER))

    async def test_preview_gateway_rejects_long_content_before_staging_or_sending(self):
        original = await self.prepare()
        with self.assertRaisesRegex(ValueError, r"1\.024"):
            await self.bot.send_preview(b"foto", original["caption"] + "x" * 1024, [])
        self.assertEqual(self.store.preview(OWNER), original)
        self.assertEqual(self.api.photo.await_count, 1)

    async def test_existing_long_preview_can_be_edited_without_regenerating_it(self):
        self.store.replace_draft(OWNER, product(), self.next_id())
        long_caption = URL + "\n\n📦 " + TITLE + OFFER_END
        preview_id = self.store.stage_preview(OWNER, CHANNEL, b"tarjeta", long_caption, [])
        self.store.activate(OWNER, preview_id, self.next_id(), "foto-ficticia")
        preview = self.store.preview(OWNER)
        await self.action("edit", preview)
        self.assertIsNotNone(self.store.editor(OWNER))
        self.assertEqual(self.store.preview(OWNER, editing=True)["caption"], long_caption)
        self.assert_native_copy()
        self.api.photo.assert_not_awaited()

    async def test_long_formatted_edit_can_be_expanded_again_up_to_1024(self):
        original, edited, current = await self.expanded_preview()
        self.assertEqual(units(edited), 500)
        self.assertEqual(current["caption"], edited)
        self.assertEqual(current["entities"], original["entities"])
        self.assertTrue(current["photo"] or current["photo_id"])
        await self.action("edit", current)
        self.assert_native_copy()
        self.assertEqual(self.store.preview(OWNER, editing=True)["caption"], edited)
        expanded = edited + "x" * (1024 - units(edited))
        await self.text(expanded, current["entities"])
        final = self.store.preview(OWNER)
        self.assertEqual(final["caption"], expanded)
        self.assertEqual(units(final["caption"]), 1024)
        self.assertEqual(final["entities"], original["entities"])

    async def test_cancel_reediting_preserves_the_long_manual_version(self):
        _, edited, current = await self.expanded_preview()
        await self.action("edit", current)
        editor = self.store.editor(OWNER)
        await self.action("cancel", {"id": editor["preview_id"], "message_id": editor["prompt"]}, prefix="e")
        self.assertIsNone(self.store.editor(OWNER))
        self.assertEqual(self.store.preview(OWNER)["caption"], edited)
        self.assertEqual(self.api.photo.await_count, 1)

    async def test_publish_preserves_all_1024_characters_of_manual_edit(self):
        _, edited, current = await self.expanded_preview(1024)
        self.assertEqual(units(current["caption"]), 1024)
        await self.action("publish", current)
        self.assertEqual(self.api.photo.call_args.args,
                         (CHANNEL, current["photo_id"], edited, current["entities"]))
        with self.assertRaises(Stale):
            self.store.preview(OWNER)

    async def test_scheduling_and_delivery_preserve_long_manual_edit(self):
        _, edited, current = await self.expanded_preview()
        self.assertEqual(current["caption"], edited)
        await self.text("/programar 04/11/2026 16:30")
        pending = self.store.pending_all((OWNER,))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["caption"], edited)
        self.assertEqual(pending[0]["entities"], current["entities"])
        self.now = pending[0]["due"]
        await self.bot.delivery.tick()
        self.assertEqual(self.api.photo.call_args.args,
                         (CHANNEL, current["photo_id"], edited, current["entities"]))

    async def test_publish_sends_the_shortened_approved_caption_and_price_format(self):
        preview = await self.prepare()
        await self.action("publish", preview)
        self.assertEqual(self.api.photo.await_count, 2)
        self.assertEqual(self.api.photo.call_args.args,
                         (CHANNEL, preview["photo_id"], preview["caption"], preview["entities"]))
        with self.assertRaises(Stale):
            self.store.preview(OWNER)

    async def test_scheduling_preserves_the_shortened_approved_caption(self):
        preview = await self.prepare()
        await self.text("/programar 04/11/2026 16:30")
        pending = self.store.pending_all((OWNER,))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["caption"], preview["caption"])
        self.assertEqual(pending[0]["entities"], preview["entities"])
        self.assertEqual(pending[0]["title"], TITLE)
        self.now = pending[0]["due"]
        await self.bot.delivery.tick()
        self.assertEqual(self.api.photo.call_args.args,
                         (CHANNEL, preview["photo_id"], preview["caption"], preview["entities"]))


if __name__ == "__main__":
    unittest.main()
