#!/usr/bin/env python3
"""tbotoverlay.py — paper-overlay signal consumer (plan NN 109, tbot-controller).

⚠️ DEPRECATED (2026-08-22, P4 cutover, session 2026-08-22-002):
SUPERSEDED by the boat-side AlphaTrendDecisionObserver
(tbot-tradingboat-red/src/tbot_tradingboat/pg_overlay/alpha_trend_decision_observer.py,
NN 119 P2/P3, live-verified 2026-08-22 — synthetic entrylong → OVR-ORDER-60 →
IB paper order). This consumer was advisory-only and is NO LONGER SCHEDULED
(no OpenClaw cron, host crontab, or LaunchAgent references it). Kept for
reference only; do not re-enable without re-review.

Reads the overlay signal JSONL produced by
skills/tbot-backtest/scripts/generate_b2_overlay_signals.py (jennie-workspace)
and prints NEW actionable overlay signals since the last check (offset state,
same pattern as check_new_signals.py). Advisory only — no orders, no TBOT
involvement, no tbot-wind-red writes.

Actionable = generator record with decision == "confirmed" (B2 + gate pass,
inside 10-15 ET execution window, outside earnings blackout). Records the
generator marks skipped_tod / skipped_blackout keep confirmed=True but
decision != "confirmed" — they must NOT be printed (critic D112.1).

Single risk bucket (critic D112.2): SMH and SOXX are >0.90 correlated. If
both emit a confirmed signal on the same trading day, at most one is kept
(the earliest bar). The loser is logged to overrides.csv with reason_code
"bucket" (per override-protocol.md) and reported in output.

Usage:
    python scripts/tbotoverlay.py [--jsonl PATH] [--raw] [--reset]
    TBOT_OVERLAY_JSONL=/path python scripts/tbotoverlay.py

Exit code 0 always (heartbeat-friendly); output = new confirmed signals.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_JSONLS = [
    Path("/Users/plusgenie/develop/jennie-workspace/skills/tbot-backtest/"
         "results/overlay/SMH_overlay_signals.jsonl"),
    Path("/Users/plusgenie/develop/jennie-workspace/skills/tbot-backtest/"
         "results/overlay/SOXX_overlay_signals.jsonl"),
]
OVERRIDES_CSV = Path("/Users/plusgenie/develop/jennie-workspace/skills/"
                     "tbot-backtest/results/overlay/overrides.csv")
OVERRIDES_HEADER = ["timestamp_et", "symbol", "bar_time_utc", "reason_code",
                    "note"]


def fmt_line(d: dict) -> str:
    ts = d.get("bar_time_et", "?")
    sizing = d.get("sizing") or {}
    qty = sizing.get("qty_est")
    ctx = d.get("context") or {}
    parts = [
        f"{ts} | {d.get('symbol', '?')} | B2+gate CONFIRMED overlay signal",
        f"close={d.get('close')} | b2_total={d.get('b2_total')} | "
        f"gate={d.get('gate_pass')}",
    ]
    if qty:
        parts.append(f"paper qty~{qty} (1% risk, 3xATR exit)")
    if ctx.get("nvda_close"):
        parts.append(f"NVDA {ctx['nvda_close']}/EMA20 {ctx.get('nvda_ema20')} "
                     f"| VIX {ctx.get('vix')} | SPY>EMA20 {ctx.get('spy_above_ema20')}")
    return " | ".join(parts)


def _bar_day_utc(d: dict) -> str:
    """Trading day (UTC date) of the signal bar, for bucket conflicts."""
    ts = d.get("bar_time_utc", "")
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts).astimezone(
            timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def log_bucket_skip(kept: dict, skipped: dict, now_et: str) -> None:
    """Append the auto-enforced bucket skip to overrides.csv (protocol doc:
    reason_code 'bucket'). Creates file + header on first write."""
    row = [now_et, skipped.get("symbol", "?"),
           skipped.get("bar_time_utc", ""), "bucket",
           f"same-day {kept.get('symbol')} kept (earlier bar) — "
           f"single semiconductor bucket, auto-enforced"]
    new = not OVERRIDES_CSV.exists()
    OVERRIDES_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OVERRIDES_CSV.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(OVERRIDES_HEADER)
        w.writerow(row)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl", action="append", type=Path, default=None,
                    help="signal JSONL(s); default = both SMH and SOXX")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    jsonls = args.jsonl or DEFAULT_JSONLS
    actionable: list[dict] = []
    printed = 0
    for jsonl in jsonls:
        if not jsonl.is_file():
            print(f"tbotoverlay: no signal file at {jsonl} "
                  f"(generator not run yet?)", file=sys.stderr)
            continue
        state = jsonl.with_name(jsonl.name + ".offset")
        offset = 0 if args.reset else _read_offset(state)
        size = jsonl.stat().st_size
        if size < offset:
            offset = 0
        if size == offset:
            continue
        with jsonl.open("r", encoding="utf-8") as fh:
            fh.seek(offset)
            tail = fh.read()
        consumed = offset + len(tail)
        if not tail.endswith("\n"):
            cut = tail.rfind("\n")
            if cut == -1:
                continue
            consumed = offset + cut + 1
            tail = tail[: cut + 1]
        _write_offset(state, consumed)
        for ln in tail.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            d = json.loads(ln)
            # D112.1: only truly actionable records — generator keeps
            # confirmed=True on skipped_tod / skipped_blackout records.
            if d.get("decision") != "confirmed":
                continue
            actionable.append(d)
    if not actionable:
        return 0

    # D112.2: single semiconductor bucket — same trading day (UTC) across
    # symbols -> keep the earliest bar, auto-log the loser.
    actionable.sort(key=lambda r: r.get("bar_time_utc", ""))
    now_et = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    kept: list[dict] = []
    day_holder: dict[str, dict] = {}
    for d in actionable:
        day = _bar_day_utc(d)
        holder = day_holder.get(day)
        if holder is None:
            day_holder[day] = d
            kept.append(d)
        else:
            # same day, second symbol -> bucket conflict
            print(f"⛔ BUCKET SKIP: {d.get('symbol')} {d.get('bar_time_et')} "
                  f"— same-day {holder.get('symbol')} kept (single "
                  f"semiconductor bucket, auto-enforced)", file=sys.stderr)
            log_bucket_skip(holder, d, now_et)

    for d in kept:
        print(d if args.raw else fmt_line(d))
        printed += 1
    if printed:
        print(f"\nReminder (advisory only): manual paper order on the paper "
              f"account; flip exit + 3xATR guardrail; VWRP core unchanged; "
              f"single semiconductor bucket (SMH/SOXX max 1 position).")
    return 0


def _read_offset(state: Path) -> int:
    try:
        return int(state.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0


def _write_offset(state: Path, offset: int) -> None:
    state.write_text(f"{offset}\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
