from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timedelta, time
from html import escape
import logging

from . import calendar_ui, ui
from .scheduled import ScheduledScreens
from .editor_ui import EditorScreens
from .preparation_ui import PreparationScreens
from .notices import NoticeManager, NoticeScreens, NoticeContext, context_for, current_product_warnings, operation_event
from .amazon import Amazon, validate_url
from .card import clean_photo, render_card
from .delivery import Delivery
from .domain import Product, compose_caption, euros, manual_labels, units, validate_caption
from .network import NetworkError
from .storage import Stale, token
from .telegram import TelegramError

logger = logging.getLogger(__name__)

HELP = """Envíame un enlace de Amazon.es o de un acortador compatible en este chat privado. Recibir un enlace nunca publica por sí solo.

/start abre el menú principal.
/help muestra los comandos manuales.
/id muestra tu identificador.
/status consulta configuración y contadores.
/datos muestra datos y advertencias del borrador.
/previa genera una vista previa con los datos del producto.
/cancelar elimina el borrador y sus controles; conserva las programaciones.

Para corregir los datos antes de generar la propuesta:
/editar texto ninguno (quita el texto para publicar solo la imagen)
/editar titulo Nombre del producto (hasta 110 caracteres)
/editar precio 29,00
/editar anterior 44,00 (o: ninguno)
/editar emoji ⌨️
/editar oferta Oferta flash (o cualquier etiqueta; alias: tipo)
/editar campaña Black Friday (o: ninguna; alias: campana)
/editar condiciones Requiere aplicar el cupón (o: ninguna)
Envía una foto para completar la imagen. Después usa /previa.

En la vista previa puedes editar libremente el texto, los enlaces y la imagen; publicar solo texto, solo imagen o ambos.
Pulsa ✏️ - Editar, copia el texto, pégalo en la caja de mensaje, modifícalo y envíalo. También puedes cambiar, sustituir, añadir o quitar la foto.
La propuesta automática cabe en un botón Copiar: se abrevia únicamente el título, incluyendo la firma en el cálculo. Si un texto editado supera 256 caracteres, cópialo desde la vista previa.

/programar DD/MM/AAAA HH:MM aprueba la vista previa vigente para esa fecha (zona configurada en TZ; próximos 30 días).
/programadas consulta las pendientes. «📝 - Borrador» abre la publicación completa.
/anular ID cancela una publicación pendiente.

Las modificaciones de una programada requieren Guardar. Reprogramar requiere Confirmar. Hasta entonces se conserva la versión anterior. Cerrar descarta los cambios sin guardar; Cancelar anula el envío o elimina un borrador nuevo.
En las tarjetas con plantilla, editar precios recalcula el descuento y editar solo el porcentaje recalcula el precio actual usando el anterior. Las etiquetas personalizadas se sincronizan sin aplicar cupones automáticamente. Si precios y descuento no coinciden, elige qué conservar.
Los borradores y las vistas sin confirmar desaparecen al caducar. Las programaciones confirmadas se conservan.

⚠️ Los precios de las publicaciones programadas no se actualizan automáticamente. Si publicas inmediatamente una oferta ya programada, se cancelará su envío futuro para evitar duplicados.
"""

MENU_ACTIONS = {
    "🛒 - Preparar oferta": "prepare",
    "📝 - Borrador": "draft",
    "📅 - Programadas": "pending",
    "👥 - Usuarios": "users",
    "📊 - Estado": "status",
    "❓ - Ayuda": "help",
    "ℹ️ - Acerca de": "about",
    "❌ - Cerrar": "close",
}


def start_keyboard():
    return [[{"text": label, "callback_data": f"m:{action}"}]
            for label, action in MENU_ACTIONS.items()]


def main_keyboard(preview_id):
    def b(label, action):
        return {"text": label, "callback_data": f"p:{preview_id}:{action}"}
    return [[b("✅ - Publicar", "publish"), b("🕒 - Programar", "schedule")],
            [b("✏️ - Editar", "edit"), b("🚫 - Cancelar", "discard")],
            [b("❌ - Cerrar", "close")]]


class Service(ScheduledScreens, EditorScreens, PreparationScreens, NoticeScreens):
    def __init__(self, config, store, telegram, amazon=None):
        self.config, self.store, self.api = config, store, telegram
        self.amazon = amazon or Amazon(config)
        self.delivery = Delivery(config, store, telegram)
        self.notices = NoticeManager(store, telegram)
        self._notice_context = ContextVar('notice_context', default=NoticeContext('session', 'input'))
        self._current_owner = ContextVar("current_owner", default=config.owner)

    @property
    def owner(self) -> int:
        return self._current_owner.get()

    def authorized(self, message, sender) -> bool:
        chat = message.get("chat", {})
        return bool(sender.get("id") in self.config.owners and chat.get("id") == sender.get("id") and chat.get("type") == "private")

    async def say(self, text, keyboard=None, entities=None, *, reply_markup=None):
        return await self.api.message(self.owner, text, keyboard, entities, reply_markup=reply_markup)

    async def remember_screens(self, section, message_ids):
        # Register the successful replacement before attempting to retire old screens.
        for old in self.store.register_screens(self.owner, section, message_ids):
            await self.remove(old)

    async def clear_screens(self, section, keep=()):
        for old in self.store.screen_messages(self.owner, section):
            if old not in keep:
                await self.remove(old)

    async def answer(self, query, text="", alert=False):
        await self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=text[:180], show_alert=alert)

    async def remove(self, message_id):
        if not message_id:
            return
        try:
            await self.api.call("deleteMessage", chat_id=self.owner, message_id=message_id)
            self.store.keep_message(self.owner, message_id)
            self.store.forget_publication_card(self.owner, message_id)
            self.store.forget_repeat_warning(self.owner, message_id)
            self.store.forget_screen(self.owner, message_id)
        except (TelegramError, NetworkError):
            logger.info("Telegram no permitió retirar un mensaje auxiliar.")

    async def strip_controls(self, message_id):
        if not message_id:
            return
        try:
            await self.api.call("editMessageReplyMarkup", chat_id=self.owner, message_id=message_id,
                                reply_markup={"inline_keyboard": []})
        except (TelegramError, NetworkError):
            logger.info("Controles ya invalidados en el servidor; no se pudo retirar el teclado.")

    def enabled(self):
        if not self.config.enabled("ENABLE_PUBLISH"):
            raise ValueError("Publicación desactivada. Activa ENABLE_PUBLISH en Docker Compose antes de publicar o programar.")

    async def handle(self, update):
        query = update.get("callback_query")
        message = query.get("message", {}) if query else update.get("message", {})
        sender = query.get("from", {}) if query else message.get("from", {})
        if not self.authorized(message, sender):
            if query:
                try:
                    await self.answer(query, "Sin permiso: solo usuarios autorizados en su chat privado.", True)
                except (TelegramError, NetworkError):
                    pass
            return
        owner_token = self._current_owner.set(sender["id"])
        notice_token = self._notice_context.set(context_for(self.store, self.owner, message, query))
        try:
            if query:
                handled = await self.callback(query)
                if handled is not False:
                    await self.resolve_notice_events(operation_event(self._notice_context.get()))
            elif message.get("text", "").startswith("/"):
                await self.command(message)
            elif message.get("text", "") in MENU_ACTIONS:
                await self.menu_action(MENU_ACTIONS[message["text"]])
            else:
                await self.receive(message)
        except (ValueError, Stale) as exc:
            if query:
                await self.answer(query, str(exc), True)
            else:
                try:
                    await self.notices.error(self.owner, str(exc), self._notice_context.get())
                except (TelegramError, NetworkError):
                    logger.warning('No se pudo entregar un aviso de validación.')
        except (TelegramError, NetworkError):
            logger.warning("No se pudo completar una interacción privada con Telegram.")
            try:
                ctx = self._notice_context.get()
                outcome_sensitive = ctx.operation in ('/anular', '/programar', '/cancelar') or ctx.operation.endswith((':publish', ':save', ':confirm'))
                await self.notices.show(self.owner, ctx.scope, operation_event(ctx),
                    "Telegram no ha permitido completar la acción. El estado guardado sigue protegido; consulta /datos, /previa o /programadas antes de repetirla.",
                    [operation_event(ctx)], kind='critical' if outcome_sensitive else 'verify', input_id=ctx.input_id)
            except (TelegramError, NetworkError):
                pass
        finally:
            self._notice_context.reset(notice_token)
            self._current_owner.reset(owner_token)

    async def details(self):
        draft = self.store.draft(self.owner)
        p = self.current_notice_product()
        try:
            p.validate(bool(draft['photo']))
            complete = True
        except ValueError:
            complete = False
        warnings = current_product_warnings(p, complete)
        repeat_warning = [warning] if (warning := self.repeat_warning(p)) else []
        lines = [f"Título: {p.title or 'pendiente'}", f"Precio EUR: {p.price or 'pendiente'}",
                 f"Referencia EUR: {p.reference or 'ausente; se omite descuento'}",
                 "Foto: " + ("disponible" if draft["photo"] or p.image_url else "pendiente"),
                 f"ASIN: {p.asin or 'sin confirmar'}", "Tipo de oferta: " + (" · ".join(p.deals) or "sin identificar"),
                 "Campaña: " + (" · ".join(p.campaigns) or "sin identificar"),
                 "Condiciones: " + (" · ".join(p.conditions) or "sin condiciones adicionales identificadas; comprueba la ficha"),
                 *warnings, *repeat_warning, "Revisa variante, vendedor, envío, disponibilidad y condiciones antes de aprobar."]
        # Split technical information at line boundaries without truncating it.
        chunk = ""
        messages = []
        for line in lines:
            for start in range(0, len(line), 1800):
                piece = line[start:start + 1800]
                if units(chunk + piece) > 3800:
                    sent = await self.say(chunk)
                    messages.append(sent["message_id"])
                    self.store.add_aux(self.owner, sent["message_id"])
                    self.store.track_message(self.owner, sent["message_id"])
                    chunk = ""
                chunk += piece + "\n"
        if chunk:
            sent = await self.say(chunk.rstrip())
            messages.append(sent["message_id"])
            self.store.add_aux(self.owner, sent["message_id"])
            self.store.track_message(self.owner, sent["message_id"])

        await self.remember_screens("datos", messages)

    def repeat_warning(self, product):
        repeat = self.store.recent_product(product.to_dict(), self.config.integer("REPEAT_WARNING_DAYS"))
        if repeat == "pendiente":
            return "⚠️ Este producto ya está programado. Puedes continuar si quieres."
        if repeat == "publicado":
            return f"⚠️ Este producto ya se publicó en los últimos {self.config['REPEAT_WARNING_DAYS']} días. Puedes continuar si quieres."
        return None

    def schedule_scope(self):
        return self.config.owners if self.config.shared_schedule else (self.owner,)

    async def replace_screen(self, message_id, text, keyboard, entities=None, *, media=False):
        """Update the current Telegram surface without sending another message."""
        params = {"chat_id": self.owner, "message_id": message_id,
                  "reply_markup": {"inline_keyboard": keyboard}}
        try:
            if media:
                return await self.api.call("editMessageCaption", caption=text, caption_entities=entities or [], **params)
            return await self.api.call("editMessageText", text=text, entities=entities or [], **params)
        except TelegramError as exc:
            if not exc.not_modified:
                raise

    async def replace_photo(self, message_id, post, keyboard):
        media = {"type": "photo", "media": post["photo_id"] or "attach://photo",
                 "caption": post["caption"], "caption_entities": post["entities"]}
        params = {"chat_id": self.owner, "message_id": message_id,
                  "media": media, "reply_markup": {"inline_keyboard": keyboard}}
        if post["photo_id"]:
            return await self.api.call("editMessageMedia", **params)
        return await self.api.call("editMessageMedia", upload=post["photo"], **params)

    async def screen(self, name, body="", keyboard=None, *, query=None, back="home"):
        text, entities = ui.section(name, body)
        keyboard = ui.navigation(back) if keyboard is None else keyboard
        if query:
            message_id = query["message"]["message_id"]
            await self.replace_screen(message_id, text, keyboard, entities)
            sent = {"message_id": message_id}
        else:
            sent = await self.say(text, keyboard, entities)
        await self.remember_screens(name, [sent['message_id']])
        return sent

    async def home(self, query=None):
        scope = self.schedule_scope()
        pending, _ = self.store.status_counts(scope)
        today = datetime.fromtimestamp(self.store.now(), self.config.timezone).date()
        start = datetime.combine(today, time.min, self.config.timezone).timestamp()
        end = datetime.combine(today + timedelta(days=1), time.min, self.config.timezone).timestamp()
        sent = self.store.sent_between(scope, start, end)
        text, entities = ui.rich(ui.HEADER + f"\nPublicadas hoy: <b>{sent}</b> • Programadas: <b>{pending}</b>")
        if query:
            await self.replace_screen(query["message"]["message_id"], text, start_keyboard(), entities)
            message_id = query["message"]["message_id"]
        else:
            sent = await self.say(text, start_keyboard(), entities)
            message_id = sent["message_id"]
        await self.remember_screens("home", [message_id])
        await self.clear_screens('menu-closed')

    async def show_status(self, query=None):
        pending, sent = self.store.status_counts(self.schedule_scope())
        await self.screen("📊 - Estado", escape(
            f"📤 Publicación: {'activada' if self.config.enabled('ENABLE_PUBLISH') else 'desactivada'}\n"
            f"🔎 Extracción de Amazon: {'activada' if self.config.enabled('AMAZON_FETCH_HTML') else 'desactivada'}\n"
            f"🕒 Zona horaria: {self.config['TZ']}\n🎯 Destino: {self.config.channel}\n"
            f"👥 Usuarios autorizados: {', '.join(str(owner) for owner in self.config.owners)}\n"
            f"📅 Programadas pendientes: {pending}\n✅ Publicaciones enviadas: {sent}"), query=query)

    async def show_pending(self, query=None):
        async with self.delivery.card_lock:
            await self._show_pending(query)
        await self.notices.resolve_event(self.owner, 'schedule-list')

    async def _show_pending(self, query=None):
        rows = self.store.pending_all(self.schedule_scope())
        if not rows:
            await self.screen("📅 - Programadas", "📅 No hay publicaciones programadas pendientes.", query=query)
            return
        for old in self.store.pending_query_cards(self.owner):
            post = self.store.get_post(old['post_id'])
            if post and post['status'] == 'pending' and post['scheduled']:
                try:
                    await self.api.call('deleteMessage', chat_id=self.owner, message_id=old['message_id'])
                except (TelegramError, NetworkError):
                    logger.info('Telegram no permitió retirar una tarjeta de programadas.')
                    continue
                self.store.keep_message(self.owner, old['message_id'])
                self.store.forget_publication_card(self.owner, old['message_id'])
                self.store.close_scheduled_view(self.owner, old['token'])
        rows = self.store.pending_all(self.schedule_scope())
        if not rows:
            await self.screen("📅 - Programadas", "📅 No hay publicaciones programadas pendientes.", query=query)
            return
        await self.clear_screens('📅 - Programadas', keep=([query['message']['message_id']] if query else []))
        for row in rows:
            state = {'media': bool(row['photo'] or row['photo_id'])}
            view = self.store.add_scheduled_view(self.owner, row['id'], row['revision'], 'card', state, self.schedule_scope())
            keyboard = [[self.scheduled_button('📝 - Borrador', view, 'open'),
                         self.scheduled_button('🚫 - Cancelar', view, 'cancel')]]
            content = {**self.content(row), 'caption': self.current_confirmation_text(row), 'entities': []}
            message_id = await self.write_content(content, keyboard)
            self.store.bind_scheduled_view(self.owner, view, message_id)

    async def menu_action(self, action, query=None):
        if action == "prepare":
            sent = await self.screen("🛒 - Preparar oferta", "🛒 Para preparar la oferta, envíame un enlace de Amazon.es o de un acortador compatible.", query=query)
            session = self.store.preparation(self.owner)
            await self.clear_preparation(keep_screen=bool(session and session['message_id'] == sent['message_id']))
            self.store.set_preparation(self.owner, sent['message_id'])
        elif action == "draft":
            await self.show_draft(query)
        elif action == "pending":
            await self.show_pending(query)
        elif action == "users":
            schedule_access = ("puedes ver y anular las de todos." if self.config.shared_schedule
                               else "puedes ver y anular solo las tuyas.")
            publication_access = ("puedes preparar y publicar ofertas." if self.config.enabled("ENABLE_PUBLISH")
                                  else "puedes preparar ofertas; la publicación está desactivada.")
            await self.screen("👥 - Usuarios", escape(
                f"Tu ID: {self.owner}\nUsuarios autorizados: {len(self.config.owners)}\n"
                f"Publicación: {publication_access}\nProgramaciones: {schedule_access}"), query=query)
        elif action == "status":
            await self.show_status(query)
        elif action == "help":
            await self.screen("❓ - Ayuda", ui.HELP_BODY, query=query)
        elif action == "about":
            await self.screen("ℹ️ - Acerca de", "👾 Conoce el proyecto y encuentra más contenido de DanieloTech.", ui.about_keyboard(), query=query)
        elif action == "version":
            await self.screen("🔢 - Versión", ui.version_body(), query=query, back="about")
        elif action == "home":
            await self.home(query)
        elif action == "close":
            sent = await self.say("Menú cerrado. Usa /start para abrirlo de nuevo.")
            await self.remember_screens('menu-closed', [sent['message_id']])

    async def command(self, message):
        successful = await self.execute_command(message)
        if successful is not False:
            command, _, value = message['text'].partition(' ')
            command = command.split('@')[0].lower()
            await self.resolve_notice_events('recognized', scope='session')
            if value.strip() or command not in ('/editar', '/programar', '/anular'):
                events = ['command:' + command, operation_event(self._notice_context.get())]
                if command == '/editar':
                    field = value.strip().partition(' ')[0]
                    field = {'tipo': 'oferta', 'campana': 'campaña'}.get(field, field)
                    if field != 'texto':
                        events.append('field:' + field)
                        await self.resolve_content_notices('field:' + field)
                elif command == '/programar':
                    events.append('scheduled')
                await self.resolve_notice_events(*events, abandon=command == '/cancelar')
                if command == '/anular':
                    await self.resolve_notice_events('command:/anular', scope='session')
            if value.strip():
                await self.clear_screens('usage:' + command)
            await self.remove(message['message_id'])
        return successful is not False

    async def execute_command(self, message):
        text = message["text"]
        command, _, value = text.partition(" ")
        command = command.split("@")[0].lower()
        value = value.strip()
        if command in ('/editar', '/programar', '/anular') and not value:
            usage = {
                '/editar': 'Uso: /editar titulo|precio|anterior|emoji|oferta|campaña|condiciones valor',
                '/programar': f'Uso: /programar DD/MM/AAAA HH:MM ({self.config.timezone.key}).',
                '/anular': 'Uso: /anular ID (consulta /programadas).',
            }
            await self.screen('usage:' + command, escape(usage[command]))
            return
        if command == "/start":
            # Retira cualquier teclado persistente antes de mostrar el menú en el chat.
            old_keyboard = await self.say("Actualizando menú…", reply_markup={"remove_keyboard": True})
            try:
                await self.home()
            finally:
                await self.remove(old_keyboard["message_id"])
        elif command == "/help":
            await self.screen("❓ - Ayuda", escape(HELP))
        elif command == "/id":
            sent = await self.say(f"Tu identificador: {self.owner}")
            await self.remember_screens("id", [sent["message_id"]])
        elif command == "/status":
            await self.show_status()
        elif command == "/datos":
            await self.details()
        elif command == "/previa":
            await self.make_preview(new_message=True)
        elif command == "/cancelar":
            previous, auxiliary = self.old_controls()
            self.store.discard(self.owner)
            for msg in [previous, *auxiliary]:
                await self.remove(msg)
            sent = await self.say("Borrador descartado. Las publicaciones programadas se conservan; usa /anular ID para cancelarlas.")
            await self.remember_screens("confirmation:/cancelar", [sent["message_id"]])
        elif command == "/editar":
            await self.edit_data(value)
        elif command == "/programar":
            self.enabled()
            due = calendar_ui.parse_manual(value, self.store.now(), self.config.timezone)
            preview = self.store.preview(self.owner)
            _, auxiliary = self.old_controls()
            post_id = self.store.consume(self.owner, preview["id"], preview["message_id"], self.config.channel, due=due)
            await self.strip_controls(preview["message_id"])
            await self.schedule_confirmation(post_id, message_id=preview["message_id"], media=bool(preview["photo"] or preview["photo_id"]))
            for msg in auxiliary:
                if msg != preview["message_id"]:
                    await self.remove(msg)
        elif command == "/programadas":
            await self.show_pending()
        elif command == "/anular":
            if not value or " " in value:
                raise ValueError("Uso: /anular ID (consulta /programadas).")
            allowed = self.config.owners if self.config.shared_schedule else (self.owner,)
            post = self.store.get_post(value)
            async with self.delivery.card_lock:
                cancelled = self.store.cancel_post(self.owner, value, scheduled_only=True, allowed_owners=allowed)
                if cancelled and post:
                    await self.invalidate_scheduled(post)
                    await self.delivery.notify()
                    cards = self.store.cancellation_cards(self.owner, value)
                    if not cards:
                        await self.say(f"🚫 Cancelada. ID: {value}")
                    elif not any(c['terminal_state'] == 'cancelled' for c in cards):
                        raise ValueError("La cancelación está guardada, pero no he podido actualizar su tarjeta. Consulta /programadas.")
                else:
                    raise ValueError("No existe una publicación pendiente con ese ID; podría estar enviada o ya en curso.")
        else:
            raise ValueError("Comando no reconocido. Usa /help.")

    def old_controls(self):
        registered = self.store.screen_messages(self.owner, 'draft-preview')
        try:
            draft = self.store.draft(self.owner, live=False)
        except Stale:
            return None, registered
        editor = self.store.editor(self.owner)
        auxiliary = [*draft["aux"], *(editor["aux"] if editor else []),
                     *self.store.repeat_warning_messages(self.owner)]
        picker = self.store.current_picker(self.owner)
        if picker:
            auxiliary.append(picker["message_id"])
        try:
            preview = self.store.preview(self.owner, editing=bool(editor))
            previous = preview['message_id']
        except Stale:
            with self.store.connect() as db:
                row = db.execute('SELECT message_id FROM previews WHERE owner=? ORDER BY rowid DESC LIMIT 1', (self.owner,)).fetchone()
            previous = row[0] if row else None
        # Invalidating controls must not lose the IDs of still-visible draft cards.
        auxiliary += [message_id for message_id in registered if message_id != previous]
        return previous, list(dict.fromkeys(auxiliary))

    async def edit_data(self, value):
        field, separator, raw = value.partition(" ")
        if field == 'texto' and separator:
            caption = '' if raw.strip().casefold() in ('ninguno', 'ninguna') else raw
            scheduled = self.store.scheduled_editor(self.owner)
            editor = self.store.editor(self.owner)
            if scheduled and (not editor or scheduled['message_id'] > editor['prompt']):
                post = self.store.scheduled_post(scheduled['post_id'], scheduled['revision'], self.owner, self.schedule_scope())
                content = {**scheduled['state']['content'], 'caption': '', 'entities': []} if not caption else (await self.text_input(caption, [], scheduled['state']['content']))[0]
                await self.scheduled_review(scheduled, post, content)
            else:
                saved = self.store.saved_preview(self.owner)
                if not saved:
                    raise Stale('Usa /previa antes de editar el texto de la publicación.')
                content = {**self.content(saved), 'caption': '', 'entities': []} if not caption else (await self.text_input(caption, [], self.content(saved)))[0]
                await self.finish_draft_edit(content)
            await self.resolve_content_notices('caption', content)
            return
        if not separator or not raw.strip():
            raise ValueError("Uso: /editar titulo|precio|anterior|emoji|oferta|campaña|condiciones valor")
        draft = self.store.draft(self.owner)
        p = Product(**draft["data"])
        if field == "titulo":
            if len(raw) > 110:
                raise ValueError("El título manual admite hasta 110 caracteres. Acórtalo sin perder los datos esenciales.")
            p.title = raw
        elif field in ("precio", "anterior"):
            amount = "" if field == "anterior" and raw.casefold() == "ninguno" else euros(raw)
            setattr(p, "price" if field == "precio" else "reference", amount)
        elif field == "emoji":
            if len(raw) > 12 or "\n" in raw:
                raise ValueError("El emoji admite hasta 12 caracteres en una línea.")
            p.emoji = raw
        elif field in ("oferta", "tipo"):
            p.custom_label = "" if raw.casefold() in ("ninguno", "ninguna") else raw
        elif field in ("campaña", "campana"):
            p.campaigns = manual_labels(raw, campaign=True)
        elif field == "condiciones":
            if len(raw) > 200:
                raise ValueError("Las condiciones manuales admiten hasta 200 caracteres; conserva sus requisitos importantes.")
            p.conditions = [] if raw.casefold() in ("ninguno", "ninguna") else [raw]
        else:
            raise ValueError("Campo desconocido. Consulta /help.")
        p.evidence[field] = "Confirmado manualmente por el propietario"
        old_message, aux = self.old_controls()
        self.store.update_draft(self.owner, p)
        await self.strip_controls(old_message)
        for msg in aux:
            await self.remove(msg)
        self.store.clear_aux(self.owner)
        sent = await self.say("Dato guardado. Usa /previa para revisar y aprobar la nueva versión.")
        self.store.add_aux(self.owner, sent["message_id"])
        self.store.track_message(self.owner, sent["message_id"])

    async def receive(self, message):
        editor = self.store.editor(self.owner)
        scheduled_editor = self.store.scheduled_editor(self.owner)
        if scheduled_editor and (message.get('text') is not None or message.get('photo')) and (not editor or scheduled_editor['message_id'] > editor['prompt']):
            await self.receive_scheduled_edit(message, scheduled_editor)
            return
        if editor and (message.get('text') is not None or message.get('photo')):
            await self.receive_draft_edit(message, editor)
            return
        if message.get("photo"):
            draft = self.store.draft(self.owner)
            chosen = message["photo"][-1]
            limit = self.config.integer("MAX_DOWNLOAD_BYTES")
            if chosen.get("file_size", 0) > limit:
                raise ValueError("La foto supera MAX_DOWNLOAD_BYTES; envía una más pequeña.")
            data = await self.api.download_photo(chosen["file_id"], limit)
            if len(data) > limit:
                raise ValueError("La foto supera el límite de descarga.")
            photo = await asyncio.to_thread(clean_photo, data, self.config.integer("MAX_IMAGE_PIXELS"))
            product = Product(**draft["data"])
            product.evidence["foto"] = "Fotografía aportada por el propietario"
            old_message, auxiliary = self.old_controls()
            self.store.update_draft(self.owner, product, photo)
            await self.strip_controls(old_message)
            for msg in auxiliary:
                await self.remove(msg)
            self.store.clear_aux(self.owner)
            sent = await self.say("Foto guardada. Usa /previa.")
            self.store.add_aux(self.owner, sent["message_id"])
            self.store.track_message(self.owner, sent["message_id"])
            await self.resolve_content_notices('image')
            return
        original = message.get("text", "").strip()
        try:
            validate_url(original)
        except ValueError:
            await self.warn_invalid_link(message)
            return
        await self.clear_preparation()
        product = await self.amazon.extract(original)
        old_message, old_aux = self.old_controls()
        self.store.replace_draft(self.owner, product, message["message_id"])
        await self.resolve_notice_events('new-draft', scope='session')
        await self.resolve_notice_events('new-draft', abandon=True)
        ctx = self._notice_context.get()
        self._notice_context.set(NoticeContext('draft:' + self.store.draft(self.owner, live=False)['id'], ctx.operation, ctx.field, ctx.input_id))
        await self.remove(old_message)
        # Preserve old useful messages until the new draft has been saved.
        for msg in old_aux:
            await self.remove(msg)
        self.store.track_message(self.owner, message["message_id"])
        await self.details()
        if product.title and product.price and product.image_url:
            await self.make_preview()
        else:
            sent = await self.say("Completa los datos que falten con /editar y una foto; después usa /previa. /help muestra ejemplos.")
            self.store.add_aux(self.owner, sent["message_id"])
            self.store.track_message(self.owner, sent["message_id"])

    async def prepare_preview_content(self, draft):
        product = Product(**draft['data'])
        product.validate(bool(draft['photo']))
        caption, entities = compose_caption(product, self.config.enabled('SHOW_PROMOTION_IN_CAPTION'), self.config['PUBLICATION_FOOTER'])
        await self.resolve_notice_events('footer-verified')
        photo = draft['photo'] or await self.amazon.download(product.image_url, image=True)
        card = await asyncio.to_thread(render_card, product, photo, self.config)
        await self.resolve_notice_events('rendered')
        return dict(photo=card, photo_id='', caption=caption, entities=entities,
                    template=True, source_photo=photo, product_data=product.to_dict())

    async def make_preview(self, *, new_message=False):
        draft = self.store.draft(self.owner)
        saved = self.store.saved_preview(self.owner)
        content = self.content(saved) if saved is not None else await self.prepare_preview_content(draft)
        await self.send_preview(content['photo'], content['caption'], content['entities'], content['photo_id'],
                                template=content['template'], source_photo=content['source_photo'],
                                product_data=content['product_data'], new_message=new_message)
        if warning := self.repeat_warning(Product(**(content['product_data'] or draft['data']))):
            sent = await self.say(warning)
            self.store.add_repeat_warning(self.owner, sent['message_id'])
            self.store.track_message(self.owner, sent['message_id'])

    async def send_preview(self, card, caption, entities, file_id=None, *, template=False, source_photo=b'', product_data=None, new_message=False):
        draft = self.store.draft(self.owner)
        # Empty drafts may remain editable but cannot be published.
        if caption.strip() or card or file_id:
            validate_caption(caption, entities, has_photo=bool(card or file_id))
        old_message, auxiliary = self.old_controls()
        saved = self.store.saved_preview(self.owner)
        preview_id = self.store.stage_preview(self.owner, self.config.channel, card, caption, entities,
                                              template=template, source_photo=source_photo, product_data=product_data)
        content = dict(photo=card, photo_id=file_id, caption=caption, entities=entities)
        message_id = await self.write_content(content, main_keyboard(preview_id), message_id=None if new_message else old_message,
                                               previous_media=bool(saved and (saved['photo'] or saved['photo_id'])))
        # Telegram keeps file_id when editing media; binary data remains a safe fallback.
        self.store.activate(self.owner, preview_id, message_id, content.get('_sent_photo_id') or file_id or '')
        self.store.clear_aux(self.owner)
        for msg in set([*auxiliary, old_message] if new_message else auxiliary):
            if msg and msg != message_id:
                await self.remove(msg)
        await self.remember_screens('draft-preview', [message_id])
        await self.resolve_notice_events('preview')

    async def show_draft(self, query=None):
        try:
            draft = self.store.draft(self.owner)
        except Stale:
            await self.screen('📝 - Borrador', '📝 No hay ningún borrador pendiente.', query=query)
            return
        record = self.store.saved_preview(self.owner)
        old_message, auxiliary = self.old_controls()
        if record is not None:
            publication = self.content(record)
        else:
            product = Product(**draft['data'])
            if product.title and product.price and (draft['photo'] or product.image_url):
                publication = await self.prepare_preview_content(draft)
            else:
                caption = product.original_url + ('\n\n📦 ' + product.title if product.title else '')
                publication = dict(photo=draft['photo'] or b'', photo_id='', caption=caption, entities=[],
                                   template=False, source_photo=b'', product_data=draft['data'])
        fresh = self.store.stage_preview(self.owner, self.config.channel, publication['photo'],
                    publication['caption'], publication['entities'], template=publication['template'],
                    source_photo=publication['source_photo'], product_data=publication['product_data'])
        from .post_ui import draft_card_text
        content = {**publication, 'caption': draft_card_text(publication), 'entities': []}
        keyboard = [[{'text': '👀 - Abrir', 'callback_data': f'd:{fresh}:open'},
                     {'text': '🚫 - Cancelar', 'callback_data': f'd:{fresh}:cancel'}],
                    [{'text': '❌ - Cerrar', 'callback_data': f'd:{fresh}:close'}]]
        # The menu stays in place; only the independent draft card is replaced.
        menu_id = query['message']['message_id'] if query else None
        message_id = await self.write_content(content, keyboard)
        self.store.activate(self.owner, fresh, message_id, content.get('_sent_photo_id') or publication['photo_id'] or '')
        self.store.clear_aux(self.owner)
        for previous in set([old_message, *auxiliary]):
            if previous and previous not in (message_id, menu_id):
                await self.remove(previous)
        if menu_id is not None:
            self.store.keep_message(self.owner, menu_id)
        await self.remember_screens('draft-preview', [message_id])

    async def schedule_confirmation(self, post_id, *, message_id=None, media=False):
        post = self.store.get_post(post_id)
        content = {**self.content(post), 'caption': self.current_confirmation_text(post), 'entities': []}
        has_media = bool(post['photo'] or post['photo_id'])
        view = self.store.add_scheduled_view(self.owner, post_id, post['revision'], 'confirmation',
                                             {'media': has_media}, self.schedule_scope())
        keyboard = [[self.scheduled_button('📝 - Borrador', view, 'open'),
                     self.scheduled_button('🚫 - Cancelar', view, 'cancel')]]
        if message_id is not None and media == has_media:
            await self.replace_screen(message_id, content['caption'], keyboard, [], media=media)
        else:
            message_id = await self.write_content(content, keyboard, message_id=message_id, previous_media=media)
        self.store.bind_scheduled_view(self.owner, view, message_id)
        self.store.keep_message(self.owner, message_id)

    def picker_content(self, picker_id, state):
        body, keyboard = calendar_ui.view(picker_id, state, self.store.now(), self.config.timezone)
        text, entities = body, []
        return text, entities, keyboard

    async def callback(self, query):
        data = query.get("data", "")
        parts = data.split(":")
        message_id = query["message"]["message_id"]
        if len(parts) == 3 and parts[0] == 'n' and parts[2] == 'review':
            await self.answer(query)
            reviewed = self.store.review_notice(self.owner, parts[1], message_id)
            await self.notices.cleanup((self.owner,))
            return reviewed
        if data == "u:close":
            await self.answer(query)
            await self.clear_preparation(message_id, keep_screen=True)
            await self.remove(message_id)
            return
        if len(parts) == 2 and parts[0] == "c":
            raise Stale("⚠️ Esta programación ha cambiado. Abre de nuevo Programadas para consultar su estado.")
        if len(parts) == 3 and parts[0] == 'd':
            preview = self.store.preview(self.owner, parts[1], message_id)
            if parts[2] == 'open':
                await self.write_content(self.content(preview), main_keyboard(preview['id']), message_id=message_id,
                                         previous_media=bool(preview['photo'] or preview['photo_id']))
            elif parts[2] == 'cancel':
                _, auxiliary = self.old_controls()
                self.store.discard(self.owner, preview['id'], message_id)
                for msg in set([message_id, *auxiliary]):
                    await self.remove(msg)
                await self.resolve_notice_events(abandon=True)
            elif parts[2] in ('close', 'back'):
                for warning_id in self.store.repeat_warning_messages(self.owner):
                    await self.remove(warning_id)
                await self.remove(message_id)
                with self.store.connect() as db:
                    db.execute('UPDATE previews SET message_id=NULL WHERE id=?', (preview['id'],))
            else:
                raise Stale()
            await self.answer(query)
            return
        if len(parts) == 3 and parts[0] == "q":
            await self.scheduled_callback(query, parts[1], parts[2])
            return
        if len(parts) == 4 and parts[0] == "r":
            await self.scheduled_picker_callback(query, parts[1], parts[2], parts[3])
            return
        if len(parts) == 2 and parts[0] == "m":
            if parts[1] not in (*MENU_ACTIONS.values(), "version", "home"):
                raise Stale()
            await self.answer(query)
            if parts[1] == "close":
                await self.remove(message_id)
            elif parts[1] == "home":
                await self.clear_preparation(message_id, keep_screen=True)
                await self.home(query)
            else:
                await self.menu_action(parts[1], query)
            return
        if len(parts) == 4 and parts[0] == "s":
            await self.picker_callback(query, parts[1], parts[2], parts[3])
            return
        if len(parts) == 3 and parts[0] == 'e':
            await self.draft_editor_action(query, parts[1], parts[2])
            return
        if len(parts) != 3 or parts[0] != "p":
            raise Stale()
        preview_id, action = parts[1:]
        preview = self.store.preview(self.owner, preview_id, message_id)
        current_picker = self.store.current_picker(self.owner)
        if current_picker and current_picker["preview_id"] == preview_id:
            raise Stale("Usa los controles del selector actual o pulsa Volver para recuperar la vista previa.")
        if preview["destination"] != self.config.channel:
            raise Stale("El destino ha cambiado. Genera una nueva /previa y revísalo antes de aprobar.")
        if action in ("publish", "schedule", "edit", "discard", "close"):
            for warning_id in self.store.repeat_warning_messages(self.owner):
                await self.remove(warning_id)
        if action == 'close':
            await self.remove(message_id)
            with self.store.connect() as db:
                db.execute('UPDATE previews SET message_id=NULL WHERE id=?', (preview_id,))
            await self.answer(query)
        elif action == "publish":
            self.enabled()
            _, auxiliary = self.old_controls()
            post_id = self.store.consume(self.owner, preview_id, message_id, self.config.channel)
            await self.answer(query, "Aprobación guardada.")
            await self.strip_controls(message_id)
            await self.delivery.send(post_id)
            await self.delivery.notify()
            await self.resolve_notice_events('published', 'operation:p:publish', abandon=True)
            for msg in auxiliary:
                await self.remove(msg)
        elif action == "discard":
            _, auxiliary = self.old_controls()
            self.store.discard(self.owner, preview_id, message_id)
            await self.answer(query, "Oferta descartada.")
            await self.strip_controls(message_id)
            await self.remove(message_id)
            for msg in auxiliary:
                await self.remove(msg)
            await self.resolve_notice_events(abandon=True)
        elif action == "schedule":
            self.enabled()
            state = calendar_ui.start(self.config.channel, self.store.now(), self.config.timezone)
            state["media"] = bool(query["message"].get("photo"))
            picker_id = self.store.set_picker(self.owner, preview_id, message_id, state)
            text, entities, keyboard = self.picker_content(picker_id, state)
            await self.replace_screen(message_id, text, keyboard, entities, media=state["media"])
            await self.answer(query)
        elif action == "edit":
            await self.open_editor(query, preview)
        else:
            raise Stale()

    async def open_editor(self, query, preview):
        edit_id = token()
        instructions, keyboard = self.editor_controls(self.content(preview), f'e:{edit_id}:')
        prompt = await self.say(instructions, keyboard)
        self.store.begin_edit(self.owner, preview['id'], preview['message_id'], edit_id, prompt['message_id'], [prompt['message_id']])
        self.store.track_message(self.owner, prompt['message_id'])
        await self.strip_controls(preview['message_id'])
        await self.answer(query)

    async def picker_callback(self, query, picker_id, action, value):
        message_id = query["message"]["message_id"]
        picker = self.store.picker(self.owner, picker_id, message_id)
        preview = self.store.preview(self.owner, picker["preview_id"])
        if action == "close":
            self.store.close_picker(self.owner, picker_id, message_id, close_preview=True)
            await self.answer(query)
            await self.remove(message_id)
            await self.resolve_notice_events('schedule-abandoned')
            with self.store.connect() as db:
                db.execute('UPDATE previews SET message_id=NULL WHERE id=?', (preview['id'],))
            return
        if action == "back" and picker["state"]["screen"] == "days":
            await self.replace_screen(message_id, preview["caption"], main_keyboard(preview["id"]),
                                      preview["entities"], media=picker["state"].get("media", False))
            self.store.close_picker(self.owner, picker_id, message_id)
            await self.answer(query)
            await self.resolve_notice_events('schedule-abandoned')
            return
        self.enabled()
        calendar_ui.check_picker_zone(picker["state"], self.config.timezone)
        if action == "confirm":
            due = picker["state"].get("due")
            if picker["state"].get("screen") != "review" or due is None:
                raise ValueError("Elige primero día, hora y minutos y revisa el resumen.")
            post_id = self.store.consume(self.owner, preview["id"], preview["message_id"], self.config.channel,
                                         due=due, picker_token=picker_id)
            await self.answer(query, "Programación guardada.")
            await self.strip_controls(preview["message_id"])
            await self.strip_controls(message_id)
            await self.schedule_confirmation(post_id, message_id=message_id,
                                             media=picker["state"].get("media", False))
            await self.resolve_notice_events('scheduled', 'operation:s:confirm')
            return
        if action == "noop":
            await self.answer(query)
            return
        try:
            state = calendar_ui.transition(picker["state"], action, value, self.store.now(), self.config.timezone)
        except (KeyError, OverflowError, TypeError):
            raise ValueError("Selecciona de nuevo una fecha y hora válidas con los botones actuales.") from None
        fresh = self.store.set_picker(self.owner, preview["id"], message_id, state, previous_token=picker_id)
        text, entities, keyboard = self.picker_content(fresh, state)
        await self.replace_screen(message_id, text, keyboard, entities, media=state.get("media", False))
        await self.answer(query)
