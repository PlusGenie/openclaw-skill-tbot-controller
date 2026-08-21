#!/usr/bin/env python3
"""tbotoverlay.py — paper-overlay signal consumer (plan NN 109, tbot-controller).

Reads the overlay signal JSONL produced by
skills/tbot-backtest/scripts/generate_b2_overlay_signals.py (jennie-workspace)
and prints NEW confirmed overlay signals since the last check (offset state,
same pattern as check_new_signals.py). Advisory only — no orders, no TBOT
involvement, no tbot-wind-red writes.

Usage:
    python scripts/tbotoverlay.py [--jsonl PATH] [--raw] [--reset]
    TBOT_OVERLAY_JSONL=/path python scripts/tbotoverlay.py

Exit code 0 always (heartbeat-friendly); output = new confirmed signals.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_JSONLS = [
    Path("/Users/plusgenie/develop/jennie-workspace/skills/tbot-backtest/"
         "results/overlay/SMH_overlay_signals.jsonl"),
    Path("/Users/plusgenie/develop/jennie-workspace/skills/tbot-backtest/"
         "results/overlay/SOXX_overlay_signals.jsonl"),
]


def fmt_line(ln: str) -> str:
    d = json.loads(ln)
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl", action="append", type=Path, default=None,
                    help="signal JSONL(s); default = both SMH and SOXX")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    jsonls = args.jsonl or DEFAULT_JSONLS
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
            if not d.get("confirmed"):
                continue  # only actionable overlay signals
            print(ln if args.raw else fmt_line(ln))
            printed += 1
    if printed == 0:
        return 0
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
