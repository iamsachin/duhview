INVOICES = {}


def load_invoice(invoice_id):
    return INVOICES.get(invoice_id)
