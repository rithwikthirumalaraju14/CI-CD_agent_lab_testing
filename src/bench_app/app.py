def run(request, root):
    from decimal import Decimal

    groups = {}
    for row in request["shards"]:
        name = row["region"]
        total, count = groups.get(name, (Decimal(0), Decimal(0)))
        samples = Decimal(row["count"])
        groups[name] = total + Decimal(row["mean_ms"]) * samples, count + samples
    means = {name: total / count if count else None for name, (total, count) in groups.items()}
    approved = all(value <= Decimal(request["limit_ms"]) for value in means.values() if value is not None)
    return {"means_ms": {name: format(value, ".3f") if value is not None else None for name, value in means.items()},
            "gate": "promote" if approved else "hold"}
