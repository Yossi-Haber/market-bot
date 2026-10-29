"""Quantitative context from supplied inputs only; percentages are percentage points."""
from statistics import mean, pstdev


def peer_context(rows, groups, benchmarks):
    output, signals = [], []
    for row in rows:
        history = row.get("historical_returns", [])
        volume = row.get("historical_volumes", [])
        sigma = pstdev(history) if len(history) >= 20 else None
        benchmark = benchmarks.get(row.get("sector_etf"))
        output.append({"symbol": row["symbol"], "daily_return": row["daily_return"], "sector_etf": row.get("sector_etf"),
                       "sector_return": benchmark, "subindustry_return": benchmarks.get(row.get("subindustry_etf")),
                       "relative_return": row["daily_return"] - benchmark if benchmark is not None else None,
                       "relative_volume": row.get("volume", 0) / mean(volume) if row.get("volume") is not None and len(volume) >= 20 and mean(volume) > 0 else None,
                       "rolling_volatility": sigma, "return_anomaly": (row["daily_return"] - mean(history)) / sigma if sigma else None,
                       "metric_limitations": "Historical arrays must exclude the current session; unavailable metrics are null."})
    by_symbol = {r["symbol"]: r["daily_return"] for r in rows}
    for name, symbols in groups.items():
        available = {s: by_symbol[s] for s in symbols if s in by_symbol}
        for direction, sign in (("UP", 1), ("DOWN", -1)):
            members = {s: v for s, v in available.items() if v * sign >= 3}
            if len(members) >= 3:
                signals.append({"group": name, "direction": direction, "members": members, "causal_claim": False})
    return {"securities": output, "peer_groups": groups, "co_movement": signals}
