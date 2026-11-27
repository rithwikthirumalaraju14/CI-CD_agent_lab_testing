def run(request, root):
    from decimal import Decimal

    groups = {}
    for row in request["shards"]:
        name = row["region"]
        weighted_total, sample_count = groups.get(name, (Decimal(0), 0))
        count = row["count"]
        groups[name] = (
            weighted_total + Decimal(row["mean_ms"]) * count,
            sample_count + count,
        )

    means = {
        name: weighted_total / sample_count if sample_count else None
        for name, (weighted_total, sample_count) in groups.items()
    }
    approved = all(
        value <= Decimal(request["limit_ms"])
        for value in means.values()
        if value is not None
    )
    return {
        "means_ms": {
            name: format(value, ".3f") if value is not None else None
            for name, value in means.items()
        },
        "gate": "promote" if approved else "hold",
    }
