def summarize(rows):
    total = 0
    for r in rows:
        total += r["amount"]
    return {"count": len(rows), "total": total}
