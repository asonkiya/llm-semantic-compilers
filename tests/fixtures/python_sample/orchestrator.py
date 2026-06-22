from pricing import add_tax


def quote(price: float) -> float:
    return add_tax(price, 0.08)
