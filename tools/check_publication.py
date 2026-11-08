"""Check the files that would be published; diagnostics never include secret values."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

PRIVATE_PARTS = {'.git', '.work', '.venv', 'venv', '__pycache__', 'data', 'backups'}
TOKEN = re.compile(rb'\b(?:[0-9]{5,}:[A-Za-z0-9_-]{20,}|(?:ghp_|github_pat_|dckr_pat_)[A-Za-z0-9_\-]{16,})\b')
PERSONAL_PATH = re.compile(rb'/Users/[A-Za-z0-9_.-]+/|/private/tmp/' rb'codex-remote-attachments/')


def forbidden(name):
    path = Path(name)
    return (any(part in PRIVATE_PARTS for part in path.parts)
            or path.name == '.DS_Store'
            or (path.name.startswith('.env') and path.name != '.env.example')
            or path.name.startswith('compose.local')
            or path.name in {'docker-compose.override.yml', 'docker-compose.override.yaml'}
            or path.name.endswith(('.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.db', '.db-wal', '.db-shm', '.pyc', '.pyo')))


def publication_files(root):
    if (root / '.git').exists():
        result = subprocess.run(['git', 'ls-files', '-z'], cwd=root, check=True, capture_output=True)
        return [Path(name.decode()) for name in result.stdout.split(b'\0') if name]
    return [path.relative_to(root) for path in root.rglob('*') if path.is_file()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--private-env', type=Path, help='Optional local .env for exact private-ID/token matching; values are never printed.')
    args = parser.parse_args()
    root = args.root.resolve()
    private = []
    if args.private_env:
        for line in args.private_env.read_text().splitlines():
            key, separator, value = line.partition('=')
            if separator and key.strip() in {'TELEGRAM_BOT_TOKEN', 'TELEGRAM_OWNER_ID', 'TELEGRAM_CHANNEL_ID'}:
                value = value.strip().strip('"\'')
                if value:
                    private.extend(part.strip().encode() for part in value.split(',') if part.strip())
    failures = []
    files = publication_files(root)
    for name in files:
        if forbidden(name):
            failures.append((str(name), 'archivo local o privado'))
            continue
        path = root / name
        if path.is_symlink():
            failures.append((str(name), 'enlace simbólico; revisar contenido antes de publicar'))
            continue
        data = path.read_bytes()
        if TOKEN.search(data):
            failures.append((str(name), 'posible credencial'))
        if PERSONAL_PATH.search(data):
            failures.append((str(name), 'ruta personal absoluta'))
        if any(value in data for value in private):
            failures.append((str(name), 'coincidencia con un dato privado local'))
    for name, reason in failures:
        print(f'{name}: {reason}')
    if failures:
        return 1
    print(f'Comprobación de publicación correcta: {len(files)} archivos, sin coincidencias en las reglas verificadas.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
