from __future__ import annotations

from contextlib import closing
import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from .storage import instance_lock
from .network import NetworkError
from .telegram import Telegram, TelegramError


def backup(directory: Path, output):
    source = directory / "bot.sqlite3"
    if not source.is_file():
        raise ValueError("No existe una base de datos que respaldar en DATA_DIR.")
    # A backup can exceed /tmp's bounded tmpfs; use the persistent filesystem.
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=directory) as temporary:
        dest = Path(temporary) / "backup.sqlite3"
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src, closing(sqlite3.connect(dest)) as dst:
            src.backup(dst)
        with dest.open("rb") as stream:
            while data := stream.read(65536):
                output.write(data)


def restore(directory: Path, input_stream):
    with instance_lock(directory):
        fd, temporary = tempfile.mkstemp(prefix=".restore-", dir=directory)
        staged = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as stream:
                while chunk := input_stream.read(65536):
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            with closing(sqlite3.connect(staged)) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("PRAGMA user_version").fetchone()[0] not in (1, 2, 3, 4, 5, 6):
                    raise ValueError("El respaldo no es válido para esta versión de la aplicación.")
                if not {"drafts", "previews", "posts", "receipts"}.issubset({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}):
                    raise ValueError("El respaldo no contiene las tablas de esta aplicación.")
            for suffix in ("-wal", "-shm"):
                (directory / ("bot.sqlite3" + suffix)).unlink(missing_ok=True)
            staged.replace(directory / "bot.sqlite3")
        except sqlite3.DatabaseError:
            raise ValueError("El archivo no es un respaldo SQLite válido.") from None
        finally:
            staged.unlink(missing_ok=True)


async def discover_ids():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token or "RELLENA" in token:
        raise ValueError("Rellena TELEGRAM_BOT_TOKEN antes de consultar IDs.")
    api = Telegram(token)
    updates = await api.call("getUpdates", timeout=0, allowed_updates=["message", "channel_post", "my_chat_member"])
    found = set()
    for update in updates:
        for kind in ("message", "channel_post", "my_chat_member"):
            record = update.get(kind, {})
            chat = record.get("chat", {})
            sender = record.get("from", {})
            if chat.get("id"):
                found.add(("chat " + chat.get("type", ""), chat["id"]))
            if sender.get("id"):
                found.add(("usuario", sender["id"]))
            forwarded = record.get("forward_origin", {}).get("chat", {})
            if forwarded.get("id"):
                found.add(("canal reenviado", forwarded["id"]))
    for label, identifier in sorted(found):
        print(f"{label}: {identifier}")
    if not found:
        print("Sin eventos pendientes. Envía /start al bot, añádelo al destino y envía /id en el grupo o reenvía un mensaje del canal; después repite la consulta.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Mantenimiento dentro del contenedor; no instala herramientas en el host.")
    parser.add_argument("action", choices=("backup", "restore", "ids", "inspect"))
    args = parser.parse_args()
    directory = Path(os.getenv("DATA_DIR", "/data"))
    try:
        if args.action == "backup":
            backup(directory, sys.stdout.buffer)
        elif args.action == "restore":
            restore(directory, sys.stdin.buffer)
            print("Respaldo restaurado.")
        elif args.action == "ids":
            asyncio.run(discover_ids())
        else:
            with sqlite3.connect(f"file:{directory / 'bot.sqlite3'}?mode=ro", uri=True) as db:
                rows = db.execute("SELECT state,count(*) FROM receipts GROUP BY state").fetchall()
                print(json.dumps({"resultados": dict(rows), "integridad": db.execute("PRAGMA quick_check").fetchone()[0]}, ensure_ascii=False))
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise SystemExit(str(exc)) from None
    except (TelegramError, NetworkError):
        raise SystemExit("No se pudieron consultar los IDs. Comprueba el token, la conexión y que el polling esté detenido.") from None


if __name__ == "__main__":
    main()
