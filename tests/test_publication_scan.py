"""The publication check rejects private artifacts without echoing their contents."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'check_publication.py'


class PublicationScan(unittest.TestCase):
    def check(self, root, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--root', str(root), *args], capture_output=True, text=True)

    def test_credentials_are_rejected_without_printing_the_value(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secret = '123456:' + 'a' * 35
            (root / 'settings.py').write_text('TOKEN = ' + repr(secret))
            result = self.check(root)
            self.assertEqual(result.returncode, 1)
            self.assertIn('settings.py', result.stdout)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_private_files_are_rejected_even_if_their_contents_are_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.env').touch()
            (root / 'bot.sqlite3').touch()
            (root / 'compose.local-mac.yaml').touch()
            result = self.check(root)
            self.assertEqual(result.returncode, 1)
            for name in ['.env', 'bot.sqlite3', 'compose.local-mac.yaml']:
                self.assertIn(name, result.stdout)

    def test_public_brand_and_placeholders_are_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.env.example').write_text('TELEGRAM_BOT_TOKEN=TOKEN_DEL_BOT')
            (root / 'README.md').write_text('DanieloTech https://t.me/danielotech')
            self.assertEqual(self.check(root).returncode, 0)

    def test_known_private_id_can_be_checked_without_publishing_it(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as private:
            root = Path(temp)
            private_env = Path(private) / '.env'
            private_env.write_text('TELEGRAM_OWNER_ID=999000111')
            (root / 'example.txt').write_text('999000111')
            result = self.check(root, '--private-env', str(private_env))
            self.assertEqual(result.returncode, 1)
            self.assertNotIn('999000111', result.stdout + result.stderr)
