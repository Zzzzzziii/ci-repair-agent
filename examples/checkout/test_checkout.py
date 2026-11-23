from checkout import checkout_total


def test_discount_keeps_free_shipping():
    assert checkout_total(200, 30) == 170


def test_remote_shipping_still_applies():
    assert checkout_total(200, 30, remote=True) == 195


def test_small_order_pays_shipping():
    assert checkout_total(150, 30) == 132
