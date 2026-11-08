"""Recognize editable offer fields and reconcile prices without changing free text."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import re
from .domain import Product, display_euros, euros, restore_price_format, units
from .footer import restore_footer

AMOUNT = r'(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,2})?\s*€'
PRICES = re.compile(rf'^👉[ \t]*(?:De\s+(?P<reference>{AMOUNT})\s+a\s+)?(?P<price>{AMOUNT})[ \t]*$', re.M)
PERCENT = re.compile(r'^🔥[ \t]*(\d{1,3}(?:[.,]\d+)?)\s*%\s*de descuento[ \t]*$', re.M)
LABEL = re.compile(r'^🏷️?[^\S\n]*(.*)$', re.M)


def title_from_caption(caption):
    lines = [line.strip() for line in caption.splitlines() if line.strip()]
    product_line = next((x for x in lines if x.startswith('📦')), '')
    candidates = [product_line] if product_line else [x for x in lines if not x.startswith(('http', '🏷', '🔥', '👉', '🗣'))]
    title = candidates[0] if candidates else (lines[0] if lines else '')
    return re.sub(r'^[^\w]+', '', title).strip()


def fields(text):
    prices, percent, label = PRICES.search(text), PERCENT.search(text), LABEL.search(text)
    return {'price': euros(prices['price']) if prices else '',
            'reference': euros(prices['reference']) if prices and prices['reference'] else '',
            'discount': Decimal(percent[1].replace(',', '.')) if percent else None,
            'label': label[1].strip() if label else ''}


def replace_span(text, entities, start, end, value):
    first, last = units(text[:start]), units(text[:end])
    delta = units(value) - (last - first)
    result = []
    for entity in entities:
        item = dict(entity)
        if item['offset'] >= last:
            item['offset'] += delta
        elif item['offset'] + item['length'] > first:
            continue
        result.append(item)
    return text[:start] + value + text[end:], result


@dataclass
class Edited:
    caption: str
    entities: list
    product: Product
    conflict: tuple | None = None


def synchronize(caption, entities, original, product, *, template, footer='', choice=None):
    p = Product(**product.to_dict())
    entities = [dict(e) for e in entities]
    if template:
        before, after = fields(original), fields(caption)
        discount = after['discount']
        if discount is not None and not 0 <= discount <= 100:
            raise ValueError('El porcentaje debe estar entre 0 y 100.')
        prices_changed = any(before[k] != after[k] for k in ('price', 'reference'))
        discount_changed = before['discount'] != discount
        calculated = None
        if after['reference'] and after['price']:
            reference, price = Decimal(after['reference']), Decimal(after['price'])
            if reference > 0:
                calculated = ((reference - price) * 100 / reference).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        if prices_changed and discount_changed and discount is not None and calculated is not None and abs(calculated - discount) >= Decimal('0.5') and choice is None:
            return Edited(caption, entities, p, (int(calculated), str(discount)))
        restoring_same_discount = (before['discount'] is None and discount is not None
                                   and calculated is not None and abs(calculated - discount) < Decimal('0.5'))
        use_discount = choice == 'discount' or (discount_changed and not prices_changed and discount is not None
                                                and not restoring_same_discount)
        use_prices = choice == 'prices' or (prices_changed and not discount_changed)
        if choice != 'keep' and use_discount:
            if not after['reference']:
                raise ValueError('No hay precio anterior para calcular el precio actual. Añádelo o conserva tu edición.')
            after['price'] = str((Decimal(after['reference']) * (100 - discount) / 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
            match = PRICES.search(caption)
            if match:
                caption, entities = replace_span(caption, entities, *match.span('price'), display_euros(after['price']))
            else:
                caption += '\n\n👉 De ' + display_euros(after['reference']) + ' a ' + display_euros(after['price'])
        elif choice != 'keep' and use_prices and calculated is not None:
            discount = calculated
            replacement = f'🔥 {calculated} % de descuento'
            match = PERCENT.search(caption)
            if match:
                caption, entities = replace_span(caption, entities, *match.span(), replacement)
            # An absent percentage line stays hidden when only prices change.
        p.show_discount = PERCENT.search(caption) is not None
        p.price, p.reference, p.custom_label = after['price'], after['reference'], after['label']
        p.manual_discount = int(discount.quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if discount is not None else None
        p.title = title_from_caption(caption)
    entities = restore_price_format(caption, entities)
    entities = restore_footer(caption, entities, footer)
    return Edited(caption, entities, p)
