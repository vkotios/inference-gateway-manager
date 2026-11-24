"""Chat slot estimates and the policy for offering a count at a given context."""

from math import floor

from .footprints import CHAT, chat_fingerprint

GIB = 1024**3
UNCALIBRATED = "not enough measurements: calibrate slots"


def fit_slots(by_slots, budget, floor):
    points = [
        (int(slots) * record["context"], record["needs_bytes"])
        for slots, records in by_slots.items()
        for record in records
    ]
    fit = {"points": len(points), "budget": budget}
    if len(points) >= 2:
        low, high = min(points)[0], max(points)[0]
        if high > low and 2 * high >= 3 * low:
            mean_p = sum(p for p, _ in points) / len(points)
            mean_need = sum(need for _, need in points) / len(points)
            k = sum((p - mean_p) * (need - mean_need) for p, need in points) / sum(
                (p - mean_p) ** 2 for p, _ in points
            )
            a = mean_need - k * mean_p
            if k > 0 and a >= 0:
                margin = max(floor, max(need - (a + k * p) for p, need in points))
                return {**fit, "a": a, "k": k, "margin": margin}
    return {**fit, "reason": "not enough measurements"}


def slot_choice(by_slots, fit, default, slots, context):
    """Prefer a covering measurement that fits, then the closest one for a refusal."""
    budget = fit.get("budget")
    records = by_slots.get(str(slots), [])
    covering = [r for r in records if context is not None and r["context"] >= context]
    record = next(
        (r for r in covering if budget is not None and r["needs_bytes"] <= budget),
        covering[0] if covering else None,
    )
    if record is not None:
        need, source = record["needs_bytes"], "measured"
    elif "a" in fit and context is not None:
        need = fit["a"] + fit["k"] * (slots * context) + fit["margin"]
        source = f"estimated from {fit['points']} measurements"
    else:
        need, source = None, "default count: not measured" if slots == default else None
    if slots == default or (need is not None and budget is not None and need <= budget):
        reason = None
    elif budget is None and need is not None:
        reason = "memory budget unavailable"
    elif need is None:
        reason = UNCALIBRATED
    else:
        reason = f"needs ~{gib(need)} GiB, budget {gib(budget)} GiB"
    return {"needs_bytes": need, "source": source, "reason": reason}


def gib(value):
    return f"{floor(value / GIB * 10 + 0.5) / 10:.1f}"


def slot_profile(manager, engine, model):
    cfg = manager.config["engines"][engine]["models"][model]
    limits = cfg["slot_limits"]
    auxiliary = manager.auxiliary
    store = manager.tracker.store
    key = f"{engine}/{model}"
    context_limits = cfg.get("context_limits", {})
    lower = context_limits.get("min", cfg.get("context"))
    upper = context_limits.get("max", cfg.get("context"))
    contexts = sorted(
        {
            r.context
            for r in store.records.values()
            if r.kind == CHAT and r.key == key and r.source == "measured" and r.context is not None
        }
    )
    by_slots = {}
    for slots in range(limits["min"], limits["max"] + 1):
        records = []
        if contexts:
            _, spec, _ = manager.prepare_model(model, engine, cfg.get("context"), slots=slots)
            fingerprint = chat_fingerprint(engine, model, spec)
            # Some legacy fingerprints include context through the rendered config template.
            for context in contexts:
                record = store.measured_at(CHAT, key, fingerprint, context)
                if (
                    record is None
                    and "config_template" in spec
                    and lower is not None
                    and lower <= context <= upper
                ):
                    _, context_spec, _ = manager.prepare_model(model, engine, context, slots=slots)
                    context_fp = chat_fingerprint(engine, model, context_spec)
                    record = store.measured_at(CHAT, key, context_fp, context)
                if record is not None:
                    records.append({"context": context, "needs_bytes": record.needs_bytes})
        by_slots[str(slots)] = records
    decision = auxiliary.fit_decision([], [])
    budget = decision.budget_bytes if decision is not None else None
    floor = manager.config.get("footprints", {}).get("slot_fit_margin_bytes", 2 * GIB)
    return by_slots, fit_slots(by_slots, budget, floor)
