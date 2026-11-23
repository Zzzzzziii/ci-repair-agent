def shipping_fee(subtotal: int, *, remote: bool) -> int:
    if remote:
        return 25
    return 0 if subtotal >= 200 else 12
