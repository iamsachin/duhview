import logging

import storage

log = logging.getLogger(__name__)


class Forbidden(Exception):
    pass


class NotFound(Exception):
    pass


def get_invoice(user, invoice_id):
    invoice = storage.load_invoice(invoice_id)
    if invoice is None:
        raise NotFound()
    return invoice


def login(username, password, check):
    ok = check(username, password)
    log.info("login attempt user=%s password=%s ok=%s", username, password, ok)
    return ok
