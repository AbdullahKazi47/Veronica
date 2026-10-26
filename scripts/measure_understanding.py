"""
How well does she understand what you MEAN? `python scripts/measure_understanding.py`

Runs the paraphrase set (tests/paraphrase_set.py) through
  before — the regex fast paths alone (a miss = the model guesses with no help)
  after  — the intent layer (core/intents.py) on top of the same regexes
and prints accuracy per intent, chit-chat that wrongly triggered a tool, ambiguous lines
that were asked about, any risky action taken on an ambiguous line, and timings.
Nothing is executed: only the routing decision is looked at.

  --meaning-only   ignore the regexes (measures the matcher by itself; what the unit test uses)
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RISKY = {"SENSITIVE", "CRITICAL"}


class _NoRegex:
    """A registry view whose fast paths never fire (for --meaning-only)."""

    def __init__(self, reg):
        self._reg = reg

    def fast_match(self, text):
        return None, text

    def get(self, name):
        return self._reg.get(name)


def evaluate(router, registry, intents, chat, ambiguous, use_regex=True):
    """-> dict of results. A request counts as understood if it is routed to the right tool,
    or handed to the model with the right tool as the top candidate."""
    from veronica.core.intents import Decision  # noqa: F401
    reg = registry if use_regex else _NoRegex(registry)
    rows, times = [], []
    per: dict[str, list[int]] = {}
    for name, (tool, phrases) in intents.items():
        for p in phrases:
            d = router.decide(p, reg)
            times.append(d.ms)
            got = d.tool.name if d.kind == "tool" and d.tool is not None else ""
            hinted = d.kind == "model" and d.hint and d.hint.split("Most likely: ")[-1].startswith(tool)
            ok = got == tool or bool(hinted)
            per.setdefault(name, [0, 0])
            per[name][0] += ok
            per[name][1] += 1
            rows.append((name, p, d.kind, got or ("hint" if hinted else ""), ok))
    chat_bad = []
    for p in chat:
        d = router.decide(p, reg)
        times.append(d.ms)
        if d.kind == "tool":
            chat_bad.append((p, d.tool.name))
    asked, risky = [], []
    for p in ambiguous:
        d = router.decide(p, reg)
        times.append(d.ms)
        if d.kind == "ask":
            asked.append(p)
        if d.kind == "tool" and d.tool is not None and d.tool.permission.name in RISKY:
            risky.append((p, d.tool.name))
    total_ok = sum(v[0] for v in per.values())
    total = sum(v[1] for v in per.values())
    return {"accuracy": total_ok / max(1, total), "per": per, "rows": rows, "chat_bad": chat_bad,
            "asked": asked, "risky": risky, "ms_median": statistics.median(times) if times else 0.0,
            "ms_max": max(times) if times else 0.0, "total": total}


def baseline(registry, intents, chat, ambiguous):
    """Regex only: right tool or not."""
    per, chat_bad, risky = {}, [], []
    for name, (tool, phrases) in intents.items():
        for p in phrases:
            t, _ = registry.fast_match(p)
            per.setdefault(name, [0, 0])
            per[name][0] += bool(t and t.name == tool)
            per[name][1] += 1
    for p in chat:
        t, _ = registry.fast_match(p)
        if t is not None:
            chat_bad.append((p, t.name))
    for p in ambiguous:
        t, _ = registry.fast_match(p)
        if t is not None and t.permission.name in RISKY:
            risky.append((p, t.name))
    total_ok = sum(v[0] for v in per.values())
    total = sum(v[1] for v in per.values())
    return {"accuracy": total_ok / max(1, total), "per": per, "chat_bad": chat_bad, "risky": risky, "total": total}


def main() -> int:
    import logging
    logging.disable(logging.WARNING)
    from tests.paraphrase_set import AMBIGUOUS, CHAT, INTENTS
    from veronica.core.intent_catalogue import build_intents
    from veronica.core.intents import IntentRouter
    from veronica.safety.permissions import PermissionGate
    from veronica.tools.registry import build_default_registry
    reg = build_default_registry(PermissionGate())
    router = IntentRouter(build_intents(list(reg.all())))
    meaning_only = "--meaning-only" in sys.argv
    before = baseline(reg, INTENTS, CHAT, AMBIGUOUS)
    after = evaluate(router, reg, INTENTS, CHAT, AMBIGUOUS, use_regex=not meaning_only)
    print(f"{'intent':<16} {'before':>8} {'after':>8}")
    for name in INTENTS:
        b, a = before["per"][name], after["per"][name]
        print(f"{name:<16} {b[0]:>4}/{b[1]:<3} {a[0]:>4}/{a[1]:<3}")
    print(f"\nrouting accuracy: before {before['accuracy']:.0%}  after {after['accuracy']:.0%}  ({after['total']} phrasings)")
    print(f"chit-chat that triggered a tool: before {len(before['chat_bad'])}  after {len(after['chat_bad'])} {after['chat_bad']}")
    print(f"ambiguous lines asked about: {len(after['asked'])}/{len(AMBIGUOUS)}  risky actions: before "
          f"{len(before['risky'])} {before['risky']}  after {len(after['risky'])} {after['risky']}")
    print(f"time per decision: median {after['ms_median']:.1f} ms, max {after['ms_max']:.1f} ms")
    if "-v" in sys.argv:
        for row in after["rows"]:
            if not row[4]:
                print("  MISS", row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
