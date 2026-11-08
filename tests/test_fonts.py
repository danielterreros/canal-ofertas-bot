"""Custom font names render real cards and preserve existing font folders."""
from io import BytesIO
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageFont

from canal_ofertas_bot.card import render_card
from canal_ofertas_bot.config import Config, DEFAULTS
from canal_ofertas_bot.domain import Product


class FontTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.bundled = Path(DEFAULTS['FONT_DIR'])
        self.product = Product('https://example.com/product', 'Producto', '17.99', '19.37',
                               deals=['Oferta flash'])
        photo = BytesIO()
        Image.new('RGB', (120, 100), 'blue').save(photo, 'JPEG')
        self.photo = photo.getvalue()

    def render(self, folder=None):
        values = {**DEFAULTS, 'IMAGE_BRAND_TEXT': 'MARCA'}
        if folder is not None:
            values['FONT_DIR'] = str(folder)
        return render_card(self.product, self.photo, Config(values))

    def copy_fonts(self, generic=False, swapped=False):
        sources = ['DejaVuSans.ttf', 'DejaVuSans-Bold.ttf']
        targets = ['font-regular.ttf', 'font-bold.ttf'] if generic else sources
        if swapped:
            sources = list(reversed(sources))
        for source, target in zip(sources, targets):
            shutil.copyfile(self.bundled / source, self.folder / target)

    def test_default_fonts_still_render_without_font_dir_override(self):
        with patch('canal_ofertas_bot.card.ImageFont.truetype', wraps=ImageFont.truetype) as load:
            image = self.render()
        paths = {Path(call.args[0]) for call in load.call_args_list}
        self.assertEqual(paths, {self.bundled / 'DejaVuSans.ttf', self.bundled / 'DejaVuSans-Bold.ttf'})
        with Image.open(BytesIO(image)) as card:
            self.assertEqual(card.size, (1200, 900))
            self.assertEqual(card.format, 'JPEG')

    def test_generic_names_load_regular_and_bold_for_the_whole_card(self):
        self.copy_fonts(generic=True)
        with patch('canal_ofertas_bot.card.ImageFont.truetype', wraps=ImageFont.truetype) as load:
            image = self.render(self.folder)
        paths = {Path(call.args[0]) for call in load.call_args_list}
        self.assertEqual(paths, {self.folder / 'font-regular.ttf', self.folder / 'font-bold.ttf'})
        self.assertEqual(image, self.render())

    def test_existing_custom_folders_keep_their_rendering(self):
        self.copy_fonts()
        self.assertEqual(self.render(self.folder), self.render())

    def test_generic_fonts_take_priority_when_both_pairs_exist(self):
        self.copy_fonts()
        self.copy_fonts(generic=True, swapped=True)
        with patch('canal_ofertas_bot.card.ImageFont.truetype', wraps=ImageFont.truetype) as load:
            image = self.render(self.folder)
        self.assertEqual({Path(call.args[0]).name for call in load.call_args_list},
                         {'font-regular.ttf', 'font-bold.ttf'})
        self.assertNotEqual(image, self.render())

    def test_incomplete_generic_pair_does_not_mix_with_legacy_fonts(self):
        self.copy_fonts()
        for missing in ('font-regular.ttf', 'font-bold.ttf'):
            with self.subTest(missing=missing):
                self.copy_fonts(generic=True)
                (self.folder / missing).unlink()
                with self.assertRaisesRegex(ValueError, missing):
                    self.render(self.folder)

    def test_invalid_generic_font_reports_the_file_and_configuration(self):
        self.copy_fonts()
        self.copy_fonts(generic=True)
        (self.folder / 'font-bold.ttf').write_bytes(b'invalid font')
        with self.assertRaisesRegex(ValueError, r'font-bold\.ttf.*FONT_DIR'):
            self.render(self.folder)

    def test_empty_custom_folder_reports_font_configuration_error(self):
        with self.assertRaisesRegex(ValueError, r'FONT_DIR.*font-regular\.ttf.*font-bold\.ttf'):
            self.render(self.folder)
