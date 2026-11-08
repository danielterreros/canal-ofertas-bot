"""Text and keyboards for private bot screens (Telegram entity offsets use UTF-16)."""
from html import escape
from html.parser import HTMLParser

from . import __version__
from .domain import units

HEADER = '🔥 <b>Canal Ofertas Bot</b> • por DanieloTech'
CLOSE = [[{"text": "❌ - Cerrar", "callback_data": "u:close"}]]
SOCIALS = (
    ("▶️ - YouTube", "https://youtube.com/@danielotech?sub_confirmation=1"),
    ("📸 - Instagram", "https://instagram.com/danytc87"),
    ("🎬 - TikTok", "https://tiktok.com/@danielotech"),
    ("👥 - Facebook", "https://facebook.com/danielotech"),
    ("𝕏 - X (Twitter)", "https://x.com/danielotech"),
)


class _RichText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.entities, self.stack = [], [], []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        kind = {"b": "bold", "i": "italic", "a": "text_link"}[tag]
        entity = {"type": kind, "offset": units("".join(self.parts))}
        if tag == "a":
            entity["url"] = dict(attrs)["href"]
        self.stack.append(entity)

    def handle_endtag(self, tag):
        entity = self.stack.pop()
        entity["length"] = units("".join(self.parts)) - entity["offset"]
        self.entities.append(entity)


def rich(markup):
    parser = _RichText()
    parser.feed(markup)
    return "".join(parser.parts), sorted(parser.entities, key=lambda e: e["offset"])


def section(name, body=""):
    titles = {
        "👥 - Usuarios": "👥 Usuarios y privilegios:",
        "📊 - Estado": "📊 Estado y configuración del bot:",
        "❓ - Ayuda": "❓ Aprende a usar el bot:",
    }
    prefix = f"<b>{titles[name]}</b>\n\n" if name in titles else ""
    return rich(prefix + body.lstrip("\n"))


def navigation(back="home"):
    return [[{"text": "⬅️ - Volver", "callback_data": f"m:{back}"},
             {"text": "❌ - Cerrar", "callback_data": "u:close"}]]


def about_keyboard():
    return [[{"text": "🔢 - Versión", "callback_data": "m:version"}],
            *[[{"text": name, "url": url}] for name, url in SOCIALS],
            [{"text": "⬅️ - Volver", "callback_data": "m:home"},
             {"text": "❌ - Cerrar", "callback_data": "u:close"}]]


HELP_BODY = '''
<b>Menú principal</b>

🛒 <b>Preparar oferta:</b> envía un enlace de Amazon.es o de un acortador compatible. El bot preparará el texto y la imagen para que los revises.

📝 <b>Borrador:</b> recupera la publicación que estás preparando mientras no haya caducado.

📅 <b>Programadas:</b> consulta tus envíos pendientes y abre cada publicación para revisarla, editarla, reprogramarla o cancelarla.

👥 <b>Usuarios:</b> consulta los usuarios autorizados y tus permisos.

📊 <b>Estado:</b> consulta la configuración del bot, el destino de publicación y los contadores.

❓ <b>Ayuda:</b> muestra esta guía.

ℹ️ <b>Acerca de:</b> muestra información del proyecto y enlaces de DanieloTech.

❌ <b>Cerrar:</b> cierra el menú sin eliminar borradores ni cancelar programaciones.

<b>Preparar y revisar publicaciones</b>

👀 <b>Abrir:</b> muestra el contenido completo de un borrador.

✅ <b>Publicar:</b> envía la publicación inmediatamente.

🕒 <b>Programar / Reprogramar:</b> elige la fecha y hora y pulsa <b>Confirmar</b>. La programación anterior sigue vigente hasta confirmar la nueva.

✏️ <b>Editar:</b> modifica el texto o la imagen. Puedes publicar solo texto, solo imagen o ambos.

✅ <b>Guardar:</b> aplica los cambios de una publicación programada conservando su fecha, hora y destino.

🚫 <b>Cancelar:</b> elimina un borrador nuevo o anula un envío programado.

❌ <b>Cerrar:</b> sale de la vista sin cancelar una programación. Durante una edición, descarta los cambios sin guardar.

<b>Editar texto e imagen</b>

📋 <b>Copiar:</b> copia el texto para pegarlo, modificarlo y enviarlo. Si es demasiado largo para el botón, cópialo desde la vista previa.

➖ <b>Quitar foto:</b> elimina la imagen y conserva el texto.

📷 <b>Cambiar foto:</b> cambia la foto del producto conservando la plantilla.

🖼️ <b>Sustituir / Añadir imagen:</b> utiliza una imagen completa, sin aplicar la plantilla.

Los borradores y las vistas sin confirmar desaparecen del chat al caducar. Las programaciones ya confirmadas se conservan.

⚠️ Los precios de las publicaciones programadas no se actualizan automáticamente. Si publicas inmediatamente una oferta ya programada, se cancelará su envío futuro para evitar duplicados.

Para consultar todos los comandos manuales usa /help.'''


def version_body():
    return f'''⚙️ <i>Versión: {escape(__version__)}</i>
Desarrollado por DanieloTech

Si encuentras cualquier fallo o sugerencia contáctame:
<a href="https://youtube.com/@danielotech?sub_confirmation=1">YouTube</a> • <a href="https://instagram.com/danytc87">Instagram</a> • <a href="https://tiktok.com/@danielotech">TikTok</a> • <a href="https://facebook.com/danielotech">Facebook</a> • <a href="https://twitter.com/danielotech">X</a>

Puedes encontrar todo lo relacionado con este bot en <a href="https://github.com/danielterreros/canal-ofertas-bot">GitHub</a> o <a href="https://hub.docker.com/r/danielterreros/canal-ofertas-bot">DockerHub</a>.'''
