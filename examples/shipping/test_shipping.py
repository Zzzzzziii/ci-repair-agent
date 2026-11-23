from decimal import Decimal

from shipping import shipping_fee


def test_standard_shipping_at_threshold_is_free():
    assert shipping_fee(Decimal("200")) == Decimal("0")


def test_standard_shipping_below_threshold_is_charged():
    assert shipping_fee(Decimal("199.99")) == Decimal("12")


def test_standard_shipping_above_threshold_is_free():
    assert shipping_fee(Decimal("200.01")) == Decimal("0")


def test_remote_shipping_at_threshold_is_charged():
    assert shipping_fee(Decimal("200"), remote_area=True) == Decimal("25")
