from __future__ import annotations

from io import BytesIO
from pathlib import Path
import warnings

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .domain import Product, display_euros


def decode_photo(data: bytes, max_pixels: int) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format not in ("JPEG", "PNG", "WEBP") or source.width * source.height > max_pixels:
                    raise ValueError("Foto demasiado grande o formato distinto de JPEG, PNG o WebP.")
                # Decode large JPEGs at a useful card resolution; preserve the whole photo.
                source.draft("RGB", (1600, 1600))
                source.load()
                ImageOps.exif_transpose(source, in_place=True)
                source.thumbnail((1600, 1600), Image.Resampling.LANCZOS, reducing_gap=3.0)
                if source.mode == "RGB":
                    return source.copy()
                rgba = source.convert("RGBA")
                white = Image.new("RGBA", rgba.size, "white")
                white.alpha_composite(rgba)
                return white.convert("RGB")
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("La foto está dañada o supera el límite de píxeles; envía otra.") from None


def clean_photo(data: bytes, max_pixels: int) -> bytes:
    image = decode_photo(data, max_pixels)
    output = BytesIO()
    image.save(output, "JPEG", quality=95)
    return output.getvalue()


def render_card(product: Product, photo: bytes, config) -> bytes:
    image = decode_photo(photo, config.integer("MAX_IMAGE_PIXELS"))
    canvas = Image.new("RGB", (1200, 900), "white")
    pen = ImageDraw.Draw(canvas)

    def font(size: int, bold: bool = False):
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        try:
            return ImageFont.truetype(str(Path(config["FONT_DIR"]) / name), size)
        except OSError:
            raise ValueError("Faltan las fuentes DejaVu dentro de la imagen; revisa FONT_DIR.") from None

    def fitted(text, size, width, bold=True):
        while size > 10:
            current = font(size, bold)
            if pen.textlength(text, font=current) <= width:
                return current
            size -= 1
        raise ValueError("La marca o etiqueta es demasiado larga para la tarjeta; acórtala.")

    brand = config["IMAGE_BRAND_TEXT"]
    if brand:
        pen.text((42, 36), brand, fill=config["IMAGE_BRAND_COLOR"], font=fitted(brand, 32, 890))
    if product.show_discount and product.discount is not None:
        pen.rounded_rectangle((978, 22, 1170, 94), radius=18, fill=config["IMAGE_DISCOUNT_BADGE_COLOR"])
        pen.text((1074, 58), f"-{product.discount} %", font=font(36, True), fill="white", anchor="mm")
    top = 118
    if product.label and config.enabled("SHOW_PROMOTION_IN_IMAGE"):
        label_font = fitted(product.label, 25, 1080)
        width = int(pen.textlength(product.label, font=label_font)) + 32
        pen.rounded_rectangle((42, 104, 42 + width, 148), radius=12, fill=config["IMAGE_DEAL_TYPE_BADGE_COLOR"])
        pen.text((58, 126), product.label, font=label_font, fill="white", anchor="lm")
        top = 166
    image.thumbnail((1040, 700 - top), Image.Resampling.LANCZOS)
    canvas.paste(image, ((1200 - image.width) // 2, top + (700 - top - image.height) // 2))
    current = display_euros(product.price) if product.price else ""
    current_font = fitted(current, 86, 700 if product.reference else 1100)
    pen.text((45, 794), current, font=current_font, fill=config["IMAGE_CURRENT_PRICE_COLOR"], anchor="lm")
    if product.reference:
        x = 80 + pen.textlength(current, font=current_font)
        previous = display_euros(product.reference)
        previous_font = fitted(previous, 42, 1155 - x, bold=False)
        pen.text((x, 794), previous, font=previous_font, fill=config["IMAGE_PREVIOUS_PRICE_COLOR"], anchor="lm")
        pen.line((x, 794, x + pen.textlength(previous, font=previous_font), 794), fill=config["IMAGE_PREVIOUS_PRICE_COLOR"], width=3)
    output = BytesIO()
    canvas.save(output, "JPEG", quality=92, optimize=True)
    return output.getvalue()
