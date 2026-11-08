from __future__ import annotations

import asyncio
import logging

from .calendar_ui import when
from .post_ui import scheduled_card_text
from .network import NetworkError
from .telegram import TelegramError
from .notices import NoticeManager

logger = logging.getLogger(__name__)


class Delivery:
    def __init__(self, config, store, telegram):
        self.config, self.store, self.telegram = config, store, telegram
        self.card_lock = asyncio.Lock()
        self.notices = NoticeManager(store, telegram)

    async def send(self, post_id):
        if not self.config.enabled("ENABLE_PUBLISH"):
            return
        pending = self.store.get_post(post_id)
        if not pending or pending["owner"] not in self.config.owners:
            return
        post = self.store.claim(pending["owner"], post_id)
        if not post:
            return
        if self.store.now() - post["due"] > self.config.integer("SCHEDULE_MAX_DELAY_SECONDS"):
            self.store.finish(post_id, "missed", notice=f"No se publicó {post_id}: supera la tolerancia de retraso desde {when(post['due'], self.config.timezone)}. Revisa la oferta y prográmala de nuevo.")
            return
        try:
            if post['photo_id'] or post['photo']:
                sent = await self.telegram.photo(post['destination'], post['photo_id'] or post['photo'], post['caption'], post['entities'])
            else:
                sent = await self.telegram.message(post['destination'], post['caption'], entities=post['entities'])
        except TelegramError as exc:
            if exc.code == 429 and post["attempts"] <= self.config.integer("TELEGRAM_RETRY_LIMIT"):
                self.store.retry(post_id, exc.retry_after or 30)
            elif exc.uncertain:
                self.uncertain(post_id)
            else:
                self.store.finish(post_id, "failed", notice=f"Telegram rechazó {post_id} (código {exc.code}). Comprueba el destino y los permisos. No se ha publicado; prepara una nueva aprobación.")
        except NetworkError as exc:
            if not exc.uncertain and post["attempts"] <= self.config.integer("TELEGRAM_RETRY_LIMIT"):
                self.store.retry(post_id, min(60, 5 * 2 ** (post["attempts"] - 1)))
            elif exc.uncertain:
                self.uncertain(post_id)
            else:
                self.store.finish(post_id, "failed", notice=f"No se pudo conectar para enviar {post_id}. No se envió; revisa la conexión y prepara una nueva aprobación.")
        except asyncio.CancelledError:
            self.uncertain(post_id)
            raise
        except Exception:
            self.uncertain(post_id)
            logger.error("Error de envío registrado como incierto; ID %s", post_id)
        else:
            self.store.finish(post_id, "sent", sent.get("message_id"), f"✅ Publicada en el destino {post['destination']}. ID: {post_id}")

    def uncertain(self, post_id):
        self.store.finish(post_id, "uncertain", notice=f"Envío {post_id} con resultado incierto. Revisa el destino antes de repetirlo; no se reintentará automáticamente para evitar duplicados.")

    async def update_scheduled_cards(self, post):
        """Retry only unfinished card updates, without sending a new chat message."""
        text = scheduled_card_text(post, self.config.timezone, post["status"])
        complete = True
        for card in self.store.terminal_cards(post["id"], post["status"]):
            if card["chat_id"] not in self.config.owners:
                self.store.forget_publication_card(card["chat_id"], card["message_id"])
                continue
            field = "caption" if card["media"] else "text"
            params = {"chat_id": card["chat_id"], "message_id": card["message_id"], field: text,
                      "caption_entities" if card["media"] else "entities": [],
                      "reply_markup": {"inline_keyboard": []}}
            try:
                await self.telegram.call("editMessageCaption" if card["media"] else "editMessageText", **params)
            except TelegramError as exc:
                if not exc.not_modified:
                    if exc.code in (400, 403):
                        self.store.forget_publication_card(card["chat_id"], card["message_id"])
                    else:
                        complete = False
                    continue
            except NetworkError:
                complete = False
                continue
            self.store.keep_message(card["chat_id"], card["message_id"])
            if not self.store.card_updated(post["id"], card["chat_id"], card["message_id"], post["status"], card["generation"]):
                complete = False
        if not complete:
            logger.warning("Actualización de tarjetas pendiente; ID %s", post["id"])
        else:
            event = 'cancel-updated' if post['status'] == 'cancelled' else 'published'
            for owner in self.config.owners:
                cards = self.store.cancellation_cards(owner, post['id'])
                verified = any(c['terminal_state'] == post['status'] for c in cards)
                if not cards and post['status'] == 'cancelled' and self.store.existing_notice(owner, 'post:' + post['id'], 'cancel-update'):
                    try:
                        await self.telegram.message(owner, text)
                    except (TelegramError, NetworkError):
                        complete = False
                        continue
                    verified = True
                if verified:
                    await self.notices.resolve(owner, 'post:' + post['id'], event)
        return complete

    async def notify(self):
        for owner in self.config.owners:
            for post_id, text in self.store.notices(owner):
                post = self.store.get_post(post_id)
                if post and post["status"] in ("sent", "cancelled"):
                    if await self.update_scheduled_cards(post):
                        self.store.notice_delivered(post_id)
                    continue
                try:
                    await self.notices.show(owner, 'post:' + post_id, 'publication-result', text, kind='critical')
                except (TelegramError, NetworkError):
                    logger.warning("Aviso pendiente de entrega; ID %s", post_id)
                else:
                    self.store.notice_delivered(post_id)

    async def verify_cancel_notices(self):
        # A terminal update may have completed before its interaction error was recorded.
        for owner in self.config.owners:
            for scope in self.store.notice_scopes(owner, 'cancel-update'):
                if not scope.startswith('post:'):
                    continue
                post_id = scope[5:]
                post = self.store.get_post(post_id)
                if post and post['status'] == 'cancelled':
                    await self.update_scheduled_cards(post)
                elif not post:
                    receipt = self.store.receipt(post_id)
                    if receipt and receipt['state'] == 'cancelled':
                        try:
                            await self.telegram.message(owner, f'🚫 Cancelada. ID: {post_id}')
                        except (TelegramError, NetworkError):
                            continue
                        await self.notices.resolve(owner, scope, 'cancel-updated')

    async def tick(self):
        async with self.card_lock:
            await self._tick()

    async def _tick(self):
        if self.config.enabled("ENABLE_PUBLISH"):
            # One eligible post per tick also limits bursts after an interruption.
            for owner in self.config.owners:
                post_id = self.store.next_due(owner)
                if post_id:
                    await self.send(post_id)
        await self.notify()
        await self.verify_cancel_notices()
        await self.cleanup_expired()
        await self.notices.cleanup(self.config.owners)
        for owner in self.config.owners:
            self.store.prune(owner, self.config.integer("SCHEDULE_HISTORY_DAYS"),
                             self.config.integer("REPEAT_WARNING_DAYS"))

    async def cleanup_expired(self):
        for item in self.store.expired_messages(self.config.owners):
            try:
                await self.telegram.call('deleteMessage', chat_id=item['owner'], message_id=item['message_id'])
            except TelegramError as exc:
                if exc.code not in (400, 403):
                    continue
            except NetworkError:
                continue
            self.store.keep_message(item['owner'], item['message_id'])
            self.store.forget_publication_card(item['owner'], item['message_id'])
            self.store.forget_screen(item['owner'], item['message_id'])
