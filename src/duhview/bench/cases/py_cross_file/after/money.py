from decimal import Decimal

SYMBOLS = {"USD": "$", "EUR": "€"}


def parse_price(text, currency):
    return Decimal(text.strip().lstrip(SYMBOLS[currency]))
