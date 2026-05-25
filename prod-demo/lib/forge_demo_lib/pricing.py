from decimal import Decimal, ROUND_HALF_UP


def calculate_total(subtotal: float, tax_rate: float) -> str:
    subtotal_amount = Decimal(str(subtotal))
    tax_multiplier = Decimal("1") + Decimal(str(tax_rate))
    total = subtotal_amount * tax_multiplier
    return str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
