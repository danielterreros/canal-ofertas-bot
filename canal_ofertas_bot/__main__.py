from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import signal

from .config import Config, ConfigurationError
from .network import NetworkError
from .service import Service
from .storage import Store, instance_lock
from .telegram import Telegram, TelegramError

logger = logging.getLogger("canal_ofertas_bot")


async def delay(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def validate_destination(api: Telegram, destination: str, bot_id: int):
    try:
        chat = await api.call("getChat", chat_id=destination)
        if chat.get("type") not in ("group", "supergroup", "channel") or chat.get("is_direct_messages"):
            raise ConfigurationError("TELEGRAM_CHANNEL_ID debe señalar un grupo, supergrupo o canal de Telegram.")
        member = await api.call("getChatMember", chat_id=destination, user_id=bot_id)
    except TelegramError as exc:
        if exc.code in (400, 403):
            raise ConfigurationError("No se pudo acceder al destino: comprueba TELEGRAM_CHANNEL_ID y añade el bot a ese grupo o canal.") from None
        raise
    status = member.get("status")
    if chat["type"] == "channel":
        if status not in ("administrator", "creator"):
            raise ConfigurationError("El bot debe ser administrador del canal de destino.")
        if status != "creator" and not member.get("can_post_messages", False):
            raise ConfigurationError("Activa el permiso de publicar mensajes del bot en el canal de destino.")
        return
    if status in ("administrator", "creator"):
        return
    if status in ("left", "kicked") or (status == "restricted" and not member.get("is_member", False)):
        raise ConfigurationError("El bot no pertenece al grupo de destino: añádelo antes de arrancar.")
    if status == "member":
        permissions = chat.get("permissions", {})
    elif status == "restricted":
        permissions = member
    else:
        raise ConfigurationError("Telegram no ha confirmado los permisos del bot en el grupo de destino.")
    # Offers use sendPhoto with a caption; can_post_messages is channel-only.
    if not permissions.get("can_send_photos", False):
        raise ConfigurationError("El bot necesita permiso para enviar fotos en el grupo de destino.")


async def run(config: Config, store: Store):
    api = Telegram(config["TELEGRAM_BOT_TOKEN"], config.integer("HTTP_TIMEOUT_SECONDS"))
    identity = await api.call("getMe")
    if config.enabled("ENABLE_PUBLISH"):
        await validate_destination(api, config.channel, identity["id"])
    await api.call("deleteWebhook", drop_pending_updates=False)
    commands = [("start", "Menú principal"), ("help", "Comandos y edición manual"),
                ("id", "Mi identificador"), ("datos", "Datos del borrador"), ("editar", "Corregir datos de producto"),
                ("previa", "Crear vista previa"), ("cancelar", "Descartar borrador"),
                ("programar", "Programar con fecha y hora"), ("programadas", "Consultar pendientes"),
                ("anular", "Cancelar una pendiente"), ("status", "Estado del bot")]
    for owner in config.owners:
        try:
            await api.call("setMyCommands", commands=[{"command": name, "description": description} for name, description in commands],
                           scope={"type": "chat", "chat_id": owner})
        except TelegramError as exc:
            if exc.code not in (400, 403):
                raise
            logger.warning("No se pudo configurar el menú privado de %s; debe iniciar el bot en Telegram.", owner)
    service = Service(config, store, api)
    for owner in config.owners:
        store.recover(owner)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    async def polling():
        backoff = 1
        try:
            while not stop.is_set():
                try:
                    updates = await api.call("getUpdates", offset=store.offset(), limit=100,
                                             timeout=config.integer("TELEGRAM_POLL_TIMEOUT_SECONDS"),
                                             request_timeout=config.integer("TELEGRAM_POLL_TIMEOUT_SECONDS") + config.integer("HTTP_TIMEOUT_SECONDS"),
                                             allowed_updates=["message", "callback_query"])
                except TelegramError as exc:
                    if exc.code in (401, 403, 409):
                        logger.error("Polling detenido por Telegram (%s). Revisa token, permisos y que no haya otra instancia.", exc.code)
                        raise
                    await delay(stop, max(backoff, exc.retry_after))
                    backoff = min(30, backoff * 2)
                    continue
                except NetworkError:
                    logger.warning("Telegram no responde. La cola y los borradores siguen guardados.")
                    await delay(stop, backoff)
                    backoff = min(30, backoff * 2)
                    continue
                backoff = 1
                for update in updates:
                    if stop.is_set():
                        break
                    if update["update_id"] < store.offset():
                        continue
                    await service.handle(update)
                    store.acknowledge(update["update_id"])
        finally:
            stop.set()

    async def queue():
        try:
            while not stop.is_set():
                await service.delivery.tick()
                await delay(stop, config.integer("SCHEDULER_INTERVAL_SECONDS"))
        finally:
            stop.set()

    logger.info("Bot iniciado. %s usuario(s) autorizado(s), horario %s, publicación %s.",
                len(config.owners), config["TZ"], "activada" if config.enabled("ENABLE_PUBLISH") else "desactivada")
    tasks = [asyncio.create_task(polling()), asyncio.create_task(queue())]
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        with store.connect() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.info("Bot detenido; datos guardados.")


def main():
    os.umask(0o077)
    try:
        config = Config.from_env()
        logging.basicConfig(level=config["LOG_LEVEL"], format="%(asctime)s %(levelname)s %(message)s")
        with instance_lock(Path(config["DATA_DIR"])):
            store = Store(Path(config["DATA_DIR"]) / "bot.sqlite3", config.integer("PREVIEW_TTL_SECONDS"))
            asyncio.run(run(config, store))
    except (ConfigurationError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    except TelegramError as exc:
        raise SystemExit(f"Telegram rechazó el arranque (código {exc.code}); comprueba los datos y los permisos del bot.") from None
    except NetworkError:
        raise SystemExit("No se pudo conectar con Telegram durante el arranque. Revisa la conexión.") from None
    except Exception as exc:
        raise SystemExit(f"El bot se ha detenido por un error interno ({type(exc).__name__}). Los envíos iniciados se recuperarán como inciertos.") from None


if __name__ == "__main__":
    main()
