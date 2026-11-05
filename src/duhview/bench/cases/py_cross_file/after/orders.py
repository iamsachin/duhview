from money import parse_price


def order_total(lines):
    return sum(parse_price(line["price"]) * line["qty"] for line in lines)
