from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from .domain import DEALS, SECONDARY, Product, euros, label_aliases, normalize
from .network import HTTPS, NetworkError

URL_WARNING = "⚠️ Envía solo un enlace HTTPS de amazon.es o un acortador oficial admitido."

PRODUCT_HOSTS = {"amazon.es", "www.amazon.es", "m.amazon.es"}
SHORT_HOSTS = {"amzn.to", "amzn.eu", "link.amazon"}
IMAGE_HOSTS = {"m.media-amazon.com", "images-eu.ssl-images-amazon.com", "images-na.ssl-images-amazon.com"}


def validate_url(value: str, image: bool = False, intermediate: bool = False):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("La URL no tiene un formato válido.") from None
    allowed = IMAGE_HOSTS if image else PRODUCT_HOSTS | SHORT_HOSTS
    # A bridge used by link.amazon is accepted only immediately after that domain.
    if intermediate and not image:
        allowed = allowed | {"amzlinks.in"}
    if (not value or len(value) > 2000 or re.search(r"[\s\x00-\x20\x7f\\]", value)
            or parsed.scheme != "https" or parsed.hostname not in allowed
            or parsed.username or parsed.password or port not in (None, 443)):
        raise ValueError(URL_WARNING)
    return parsed


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    parent: Node | None = field(default=None, repr=False)
    children: list = field(default_factory=list, repr=False)

    @property
    def hidden(self) -> bool:
        return ("hidden" in self.attrs or "aok-hidden" in self.attrs.get("class", "").split()
                or bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", self.attrs.get("style", ""), re.I))
                or bool(self.parent and self.parent.hidden))

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self) -> str:
        if self.tag in ("script", "style", "noscript") or self.hidden:
            return ""
        return " ".join(" ".join(c.text() if isinstance(c, Node) else c for c in self.children).split())

    def find(self, predicate):
        return next((node for node in self.walk() if not node.hidden and predicate(node)), None)


class ProductHTML(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]
        self.count = 0
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 100000 or len(self.stack) > 180:
            raise ValueError("La ficha es demasiado compleja; completa el producto manualmente.")
        node = Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def by_id(self, value: str) -> Node | None:
        return self.root.find(lambda node: node.attrs.get("id") == value)


def read_price(node: Node) -> str:
    offscreen = node.find(lambda item: item.has_class("a-offscreen"))
    raw = offscreen.text() if offscreen else node.text()
    if "€" in raw or "EUR" in raw:
        try:
            return euros(raw)
        except ValueError:
            pass
    whole = node.find(lambda item: item.has_class("a-price-whole"))
    fraction = node.find(lambda item: item.has_class("a-price-fraction"))
    currency = node.find(lambda item: item.has_class("a-price-symbol"))
    if whole and fraction and currency and currency.text() in ("€", "EUR") and re.fullmatch(r"\d{2}", fraction.text()):
        return euros(whole.text().replace(" ", "").rstrip(",") + "," + fraction.text())
    raise ValueError("No hay un precio EUR principal inequívoco.")


def has_captcha(doc: ProductHTML) -> bool:
    """Use active HTML controls and challenge-page headings as evidence."""
    def active_nodes(node):
        if node.hidden or node.tag in ("script", "style", "template", "textarea"):
            return
        yield node
        for child in node.children:
            if isinstance(child, Node):
                yield from active_nodes(child)

    product_title = False
    challenge_heading = False
    for node in active_nodes(doc.root):
        if node.tag == "input" and (node.attrs.get("type") or "text").lower() != "hidden":
            if any((node.attrs.get(key) or "").lower() == "captchacharacters" for key in ("id", "name")):
                return True
        if node.tag == "form" and re.search(r"(?:^|/)validatecaptcha(?:[/?#]|$)", node.attrs.get("action") or "", re.I):
            return True
        if node.attrs.get("id") == "productTitle" and node.text():
            product_title = True
        if node.tag in ("title", "h1", "h2", "h3", "h4", "h5", "h6"):
            heading = normalize(node.text())
            if any(marker in heading for marker in ("robot check", "introduce los caracteres que ves",
                                                     "enter the characters you see", "type the characters you see")):
                challenge_heading = True
    return challenge_heading and not product_title


def parse_product(html: str, original_url: str, final_url: str) -> Product:
    if validate_url(final_url).hostname not in PRODUCT_HOSTS:
        raise ValueError("El acortador no termina en Amazon España.")
    product = Product(original_url)
    doc = ProductHTML(html)
    if has_captcha(doc):
        product.warnings.append("Amazon solicita CAPTCHA. No se intenta eludirlo: completa los datos manualmente.")
        return product
    title = doc.by_id("productTitle")
    if title:
        product.title = title.text()
        product.evidence["titulo"] = "Título principal de la ficha"
    match = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Za-z0-9]{10})(?:[/?]|$)", final_url)
    product.asin = match[1].upper() if match else ""
    scopes = [doc.by_id(name) for name in ("corePriceDisplay_desktop_feature_div", "corePrice_desktop", "apex_desktop")]
    for scope in filter(None, scopes):
        paying = scope.find(lambda node: node.has_class("priceToPay") or node.has_class("apexPriceToPay"))
        if paying:
            try:
                product.price = read_price(paying)
                product.evidence["precio"] = "Bloque de precio principal EUR"
                break
            except ValueError:
                continue
    if not product.price:
        for name in ("priceblock_dealprice", "priceblock_ourprice"):
            paying = doc.by_id(name)
            if paying:
                try:
                    product.price = read_price(paying)
                    break
                except ValueError:
                    pass
    for scope in filter(None, scopes[:1]):
        reference = scope.find(lambda node: node.has_class("basisPrice") or
                               (node.has_class("a-text-price") and node.attrs.get("data-a-strike") == "true"))
        if reference and product.price:
            try:
                from decimal import Decimal
                value = read_price(reference)
                if Decimal(value) > Decimal(product.price):
                    product.reference = value
                    product.evidence["referencia"] = "Precio de referencia tachado, no necesariamente histórico"
                    product.warnings.append("El precio de referencia puede ser PVP; no acredita el mínimo de 30 días.")
            except ValueError:
                pass
    image = doc.by_id("landingImage") or doc.by_id("imgBlkFront")
    if image:
        url = image.attrs.get("data-old-hires") or image.attrs.get("src", "")
        if not url:
            try:
                candidates = json.loads(image.attrs.get("data-a-dynamic-image", "{}"))
                url = next(iter(candidates), "")
            except (ValueError, TypeError):
                url = ""
        try:
            validate_url(url, image=True)
            product.image_url = url
            product.evidence["foto"] = "Fotografía principal de la ficha"
        except ValueError:
            pass
    promotion_nodes = [doc.by_id(name) for name in ("dealBadge_feature_div", "dealBadgeSupportingText")]
    for scope in filter(None, scopes[:2]):
        promotion_nodes.extend(node for node in scope.walk() if node.has_class("a-badge-text") or node.has_class("dealBadgeText"))
    inactive = re.compile(r"\b(?:finalizad[ao]s?|agotad[ao]s?|caducad[ao]s?|proximamente|proxima oferta|no disponible|sin cupon|no hay)\b")
    for node in filter(None, promotion_nodes):
        raw, evidence = node.text(), normalize(node.text())
        if node.hidden or not raw or len(raw) > 300 or inactive.search(evidence):
            continue
        for campaign, target in ((True, product.campaigns), (False, product.deals)):
            remainder = evidence
            aliases = label_aliases(campaign)
            for needle in sorted(aliases, key=len, reverse=True):
                label = aliases[needle]
                if re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", remainder):
                    if label not in target:
                        target.append(label)
                        product.evidence.setdefault("promociones", []).append(raw)
                    remainder = remainder.replace(needle, "")
    for node in doc.root.walk():
        identifier = node.attrs.get("id", "")
        if identifier not in ("couponText", "promoPriceBlockMessage_feature_div") and not node.has_class("couponLabelText"):
            continue
        raw, normalized = node.text(), normalize(node.text())
        if node.hidden or not raw or len(raw) > 600 or inactive.search(normalized):
            continue
        secondary = []
        if "cupon" in normalized or (identifier == "couponText" and re.search(r"\d\s*(?:%|€)", raw)):
            secondary.append("Cupón descuento")
        if "codigo promocional" in normalized or "codigo de descuento" in normalized:
            secondary.append("Código promocional")
        if re.search(r"(?:al comprar|compra|comprando)\s+\d|\d\s*(?:x|por)\s*\d", normalized):
            secondary.append("Promoción por cantidad")
        for label in secondary:
            if label not in product.deals:
                product.deals.append(label)
        if secondary and raw not in product.conditions:
            product.conditions.append(raw)
    if any(label in SECONDARY for label in product.deals):
        product.warnings.append("Las promociones secundarias no se han restado del precio; comprueba sus condiciones.")
    if "Oferta Prime" in product.deals:
        product.conditions.append("Requiere Prime; verifica el acceso y los requisitos de la oferta.")
    if not product.reference:
        product.warnings.append("Sin referencia fiable: no se muestran referencia ni descuento.")
    product.deals.sort(key=DEALS.index)
    return product


class Amazon:
    def __init__(self, config, transport=None):
        self.config, self.transport = config, transport or HTTPS()

    async def download(self, url: str, image: bool = False) -> bytes | tuple[bytes, str]:
        current, previous_host = url, ""
        deadline = time.monotonic() + self.config.integer("HTTP_TIMEOUT_SECONDS")
        for redirect in range(self.config.integer("MAX_REDIRECTS") + 1):
            parsed = validate_url(current, image=image, intermediate=previous_host == "link.amazon")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Se agotó el tiempo de extracción; completa los datos manualmente.")
            response = await asyncio.to_thread(self.transport.request, "GET", current,
                                               headers={"Accept-Language": "es-ES,es;q=0.9"}, timeout=remaining,
                                               max_bytes=self.config.integer("MAX_DOWNLOAD_BYTES"))
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise ValueError("El servidor no indica adónde redirigir el enlace.")
                previous_host = parsed.hostname
                current = urljoin(current, location)
                continue
            if response.status != 200:
                raise ValueError(f"Amazon devolvió HTTP {response.status}. Completa el producto manualmente.")
            kind = response.headers.get("content-type", "").split(";")[0].lower()
            if image:
                if kind not in ("image/jpeg", "image/png", "image/webp"):
                    raise ValueError("La dirección no devuelve una foto JPEG, PNG o WebP.")
                return response.body
            if parsed.hostname not in PRODUCT_HOSTS:
                raise ValueError("El enlace no termina en Amazon España.")
            if kind not in ("text/html", "application/xhtml+xml"):
                raise ValueError("La respuesta no es una ficha HTML de producto.")
            return response.body, current
        raise ValueError("El enlace tiene demasiadas redirecciones.")

    async def extract(self, original_url: str) -> Product:
        validate_url(original_url)
        if not self.config.enabled("AMAZON_FETCH_HTML"):
            return Product(original_url, warnings=["Extracción HTML desactivada. Completa título, precio y foto manualmente."])
        try:
            body, final = await self.download(original_url)
            return parse_product(body.decode("utf-8", errors="replace"), original_url, final)
        except (ValueError, NetworkError) as exc:
            return Product(original_url, warnings=[str(exc), "Puedes completar todos los datos desde Telegram."])
