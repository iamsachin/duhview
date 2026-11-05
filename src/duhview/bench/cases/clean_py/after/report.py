def _amount_total(rows):
    return sum(r["amount"] for r in rows)


def summarize(rows):
    """Count and total of the given rows."""
    return {"count": len(rows), "total": _amount_total(rows)}
