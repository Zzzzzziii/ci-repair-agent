def apply_coupon(subtotal: int, coupon: int) -> int:
    return max(0, subtotal - coupon)
