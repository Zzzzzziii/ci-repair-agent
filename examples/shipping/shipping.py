from decimal import Decimal


FREE_SHIPPING_THRESHOLD = Decimal("200")
STANDARD_SHIPPING_FEE = Decimal("12")
REMOTE_SHIPPING_FEE = Decimal("25")


def shipping_fee(subtotal: Decimal, *, remote_area: bool = False) -> Decimal:
    if remote_area:
        return REMOTE_SHIPPING_FEE
    if subtotal > FREE_SHIPPING_THRESHOLD:
        return Decimal("0")
    return STANDARD_SHIPPING_FEE
