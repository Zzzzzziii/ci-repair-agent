from discounts import apply_coupon
from delivery import shipping_fee


def checkout_total(subtotal: int, coupon: int, *, remote: bool = False) -> int:
    discounted = apply_coupon(subtotal, coupon)
    return discounted + shipping_fee(discounted, remote=remote)
