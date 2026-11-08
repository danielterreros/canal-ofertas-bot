from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_HALF_UP
import re
import unicodedata


COPY_TEXT_LIMIT = 256
CAPTION_TEXT_LIMIT = 1024


def euros(text: str) -> str:
    cleaned = re.sub(r"(?:EUR|€|\s)", "", text)
    if not re.fullmatch(r"(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,2})?", cleaned):
        raise ValueError("Introduce un precio EUR como 29,00 o 1.299,99; no un rango ni otra moneda.")
    amount = Decimal(cleaned.replace(".", "").replace(",", "."))
    if not Decimal("0") < amount <= Decimal("999999.99"):
        raise ValueError("El importe debe estar entre 0,01 y 999.999,99 EUR.")
    return str(amount.quantize(Decimal("0.01")))


def display_euros(value: str) -> str:
    return format(Decimal(value), ",.2f").translate(str.maketrans(",.", ".,")) + " €"


def normalize(value: str) -> str:
    return " ".join("".join(c for c in unicodedata.normalize("NFD", value.casefold())
                             if not unicodedata.combining(c)).split())


# User-visible vocabulary, reproduced from the observable command interface.
DEALS = ("Oferta flash", "Oferta del día", "Oferta por tiempo limitado", "Oferta destacada",
         "Mejores ofertas", "Oferta Prime", "Descuento directo", "Cupón descuento",
         "Código promocional", "Promoción por cantidad")
CAMPAIGNS = ("Rebajas de enero", "Ofertas de San Valentín", "Fiesta de Ofertas de Primavera",
             "Ofertas de Semana Santa", "Ofertas del Día de la Madre", "Ofertas de verano",
             "Prime Day", "Vuelta al cole", "Fiesta de Ofertas Prime", "Ofertas de Halloween",
             "Semana del Black Friday", "Black Friday", "Cyber Monday", "Ofertas de Navidad",
             "Ofertas de última hora para regalos", "Ofertas de fin de año")
SECONDARY = {"Cupón descuento", "Código promocional", "Promoción por cantidad"}


def label_aliases(campaign=False):
    vocabulary = CAMPAIGNS if campaign else DEALS
    result = {normalize(label): label for label in vocabulary}
    alternatives = ({
        "San Valentín": "Ofertas de San Valentín", "Semana Santa": "Ofertas de Semana Santa",
        "Día de la Madre": "Ofertas del Día de la Madre", "Halloween": "Ofertas de Halloween",
    } if campaign else {
        "ofertas flash": "Oferta flash", "ofertas del día": "Oferta del día",
        "oferta de tiempo limitado": "Oferta por tiempo limitado", "mejor oferta": "Mejores ofertas",
        "ofertas Prime": "Oferta Prime", "oferta exclusiva para Prime": "Oferta Prime",
        "oferta exclusiva para miembros Prime": "Oferta Prime", "exclusivo para Prime": "Oferta Prime",
        "con Prime": "Oferta Prime", "cupón de descuento": "Cupón descuento",
        "código de descuento": "Código promocional",
    })
    result.update({normalize(key): value for key, value in alternatives.items()})
    return result


def manual_labels(text: str, campaign: bool = False) -> list[str]:
    if normalize(text) in ("ninguno", "ninguna"):
        return []
    aliases = label_aliases(campaign)
    result = []
    for part in text.split(";"):
        label = aliases.get(normalize(part))
        if label is None:
            raise ValueError("Etiqueta no reconocida. Consulta /help; separa las etiquetas con ; o usa ninguna.")
        if label not in result:
            result.append(label)
    if len(result) > 5:
        raise ValueError("Se admiten hasta cinco etiquetas por campo.")
    return result


@dataclass
class Product:
    original_url: str
    title: str = ""
    price: str = ""
    reference: str = ""
    image_url: str = ""
    asin: str = ""
    emoji: str = "📦"
    deals: list[str] = field(default_factory=list)
    campaigns: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    custom_label: str | None = None
    manual_discount: int | None = None
    show_discount: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def discount(self) -> int | None:
        if self.manual_discount is not None:
            return self.manual_discount
        if self.reference and self.price and Decimal(self.reference) > Decimal(self.price):
            return int(((Decimal(self.reference) - Decimal(self.price)) * 100 / Decimal(self.reference))
                       .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return None

    @property
    def label(self) -> str:
        if self.custom_label is not None:
            return self.custom_label
        return next((x for x in self.deals if x not in SECONDARY), next(iter(self.campaigns), ""))

    def validate(self, has_photo: bool) -> None:
        missing = []
        if not self.title.strip():
            missing.append("título (/editar titulo …)")
        if not self.price:
            missing.append("precio (/editar precio 29,00)")
        if not (has_photo or self.image_url):
            missing.append("fotografía (envía una foto de producto)")
        if missing:
            raise ValueError("Faltan: " + ", ".join(missing) + ". Después usa /previa.")
        if self.reference and Decimal(self.reference) <= Decimal(self.price):
            raise ValueError("La referencia debe ser mayor que el precio; corrígela o usa /editar anterior ninguno.")


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def restore_price_format(text: str, entities: list[dict]) -> list[dict]:
    """Restore price styles in the final offer line, respecting manual formatting."""
    result = [dict(entity) for entity in entities]
    price = r"(?:[0-9]{1,3}(?:\.[0-9]{3})+|[0-9]+),[0-9]{2}[ \u00a0]*€"
    pattern = (
        rf"^👉[ \u00a0]+"
        rf"(?:De[ \u00a0]+(?P<previous>{price})"
        rf"[ \u00a0]+a[ \u00a0]+)?"
        rf"(?P<current>{price})[ \u00a0]*$"
    )
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        return result

    for group, kind in (("previous", "strikethrough"), ("current", "bold")):
        if match.group(group) is None:
            continue
        start = units(text[:match.start(group)])
        length = units(match.group(group))
        end = start + length
        if not any(start < entity["offset"] + entity["length"]
                   and entity["offset"] < end for entity in result):
            result.append({"type": kind, "offset": start, "length": length})
    return sorted(result, key=lambda entity: entity["offset"])


def validate_caption(text: str, entities: list[dict], *, has_photo: bool = False) -> None:
    limit = CAPTION_TEXT_LIMIT if has_photo else 4096
    if (not text.strip() and not has_photo) or units(text) > limit:
        raise ValueError(f"La publicación debe contener texto o imagen. El texto admite hasta {format(limit, ',').replace(',', '.')} caracteres "
                         "(algunos emojis cuentan como dos).")
    encoded = text.encode("utf-16-le")
    intervals = []
    for entity in entities:
        start, length = entity.get("offset"), entity.get("length")
        if not isinstance(start, int) or not isinstance(length, int) or start < 0 or length <= 0 or (start + length) * 2 > len(encoded):
            raise ValueError("El formato del mensaje contiene una entidad fuera de sus límites.")
        try:
            encoded[:start * 2].decode("utf-16-le")
            encoded[start * 2:(start + length) * 2].decode("utf-16-le")
        except UnicodeError:
            raise ValueError("El formato del mensaje divide un carácter especial.") from None
        intervals.append((start, start + length))
    for start, end in intervals:
        if any(start < other_start < end < other_end for other_start, other_end in intervals):
            raise ValueError("Hay formatos solapados incompatibles con Telegram.")


def _shorten_title(title: str, budget: int) -> str:
    if budget < 1:
        raise ValueError(f"El enlace y los datos de la oferta no dejan espacio para el título dentro de {COPY_TEXT_LIMIT} caracteres. "
                         "Envía un enlace corto oficial de Amazon para preparar la oferta.")
    if units(title) <= budget:
        return title

    available = budget - units("…")
    end = 0
    for char in title:
        width = units(char)
        if width > available:
            break
        available -= width
        end += 1
    prefix = title[:end]
    if not title[end].isspace():
        prefix = re.sub(r"\s+\S*$", "", prefix)
    return prefix.rstrip() + "…"


def compose_caption(product: Product, show_label: bool = True, footer: str = "") -> tuple[str, list[dict]]:
    from .footer import parse_footer
    footer_text, footer_entities = parse_footer(footer)
    ending: list[tuple[str, str | None]] = []
    if show_label and product.label:
        ending.append(("🏷️ " + product.label + "\n\n", None))
    if product.discount is not None:
        if product.show_discount:
            ending.append((f"🔥 {product.discount} % de descuento\n\n", None))
        ending.extend([("👉 De ", None), (display_euros(product.reference), "strikethrough"), (" a ", None)])
    else:
        ending.append(("👉 ", None))
    ending.append((display_euros(product.price), "bold"))
    if footer_text:
        ending.append(("\n\n" + footer_text, None))

    prefix = product.original_url + "\n\n" + product.emoji + " "
    fixed = prefix + "\n\n" + "".join(value for value, _ in ending)
    title = _shorten_title(product.title, COPY_TEXT_LIMIT - units(fixed))
    chunks = [prefix + title + "\n\n"]
    entities: list[dict] = []
    for value, kind in ending:
        if kind:
            entities.append({"type": kind, "offset": units("".join(chunks)), "length": units(value)})
        chunks.append(value)
    text = "".join(chunks)
    if footer_text:
        shift = units(text) - units(footer_text)
        entities.extend({**entity, "offset": entity["offset"] + shift} for entity in footer_entities)
    validate_caption(text, entities, has_photo=True)
    return text, entities
