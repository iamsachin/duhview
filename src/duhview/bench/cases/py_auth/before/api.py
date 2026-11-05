import logging

import storage

log = logging.getLogger(__name__)


class Forbidden(Exception):
    pass


def get_invoice(user, invoice_id):
    invoice = storage.load_invoice(invoice_id)
    if invoice is None or invoice["owner_id"] != user["id"]:
        raise Forbidden()
    return invoice


def login(username, password, check):
    ok = check(username, password)
    log.info("login attempt user=%s ok=%s", username, ok)
    return ok
