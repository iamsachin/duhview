from decimal import Decimal


def parse_price(text):
    return Decimal(text.strip().lstrip("$"))
