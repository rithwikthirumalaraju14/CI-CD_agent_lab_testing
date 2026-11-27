def run(request, root):
    from decimal import Decimal
    from unicodedata import normalize

    groups = {}
    for row in request["shards"]:
        # Canonically equivalent Unicode spellings identify the same region,
        # while retaining the region ID's case sensitivity.
        name = normalize("NFC", row["region"])
        total, count = groups.get(name, (Decimal(0), 0))
        shard_count = row["count"]
        groups[name] = (
            total + Decimal(row["mean_ms"]) * shard_count,
            count + shard_count,
        )

    means = {
        name: total / count if count else None
        for name, (total, count) in groups.items()
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
