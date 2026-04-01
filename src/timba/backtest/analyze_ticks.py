"""Tick analysis: EV calibration by joining tick EVs with trade outcomes.

Core question: when our formula says EV = +X, does the actual win rate match?
If EV = +0.03 (implying ~98% P(win) at buy=0.95) but actual WR is 50%, the formula is broken.
"""

import sys
from collections import defaultdict
from pathlib import Path

from timba.backtest.common import load_ticks_with_evs, load_trade_outcomes


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def _pct(n: int, total: int) -> str:
    return f"{n/total:.1%}" if total > 0 else "  n/a"


def _ev_bucket_label(ev: float) -> str:
    if ev <= 0:
        return "<=0"
    elif ev <= 0.001:
        return "0-.001"
    elif ev <= 0.005:
        return ".001-.005"
    elif ev <= 0.01:
        return ".005-.01"
    elif ev <= 0.05:
        return ".01-.05"
    elif ev <= 0.10:
        return ".05-.10"
    else:
        return ".10+"


EV_BUCKET_ORDER = ["<=0", "0-.001", ".001-.005", ".005-.01", ".01-.05", ".05-.10", ".10+"]


def analyze_ticks_main(data_dir: Path, coin: str | None = None,
                       interval: str | None = None, strategy: str = "favorite"):
    """Analyze tick data by joining with trade outcomes for EV calibration."""
    ticks_by_slug, skip_count = load_ticks_with_evs(data_dir, strategy=strategy)
    if not ticks_by_slug:
        print("No tick files found.", file=sys.stderr)
        sys.exit(1)

    # Apply filters
    if coin or interval:
        from timba.market import parse_slug
        filtered = {}
        for slug, ticks in ticks_by_slug.items():
            s_coin, s_interval = parse_slug(slug)
            if coin and s_coin != coin:
                continue
            if interval and s_interval != interval:
                continue
            filtered[slug] = ticks
        ticks_by_slug = filtered
        if not ticks_by_slug:
            filters = []
            if coin:
                filters.append(f"coin={coin}")
            if interval:
                filters.append(f"interval={interval}")
            print(f"No ticks match filter: {', '.join(filters)}", file=sys.stderr)
            sys.exit(1)

    outcomes = load_trade_outcomes(data_dir, strategy=strategy)
    total_ticks = sum(len(v) for v in ticks_by_slug.values())
    matched = sum(1 for s in ticks_by_slug if s in outcomes)

    W = sys.stdout.write
    W(f"\nLoaded {total_ticks} ticks across {len(ticks_by_slug)} markets\n")
    W(f"  Trade outcomes matched: {matched} / {len(ticks_by_slug)}\n")
    if skip_count:
        W(f"  Skipped (incomplete): {skip_count}\n")

    # ── Per-market summary: best EV + outcome ──
    # For each market, extract the "entry point" (first +EV tick) and peak EV,
    # then join with trade outcome.
    market_entries = []  # one per market that had +EV and a trade outcome

    for slug, ticks in ticks_by_slug.items():
        trade = outcomes.get(slug)
        if not trade:
            continue
        won = trade["type"].endswith("_win")
        coin = ticks[0]["coin"]
        interval = ticks[0]["interval"]

        # Find first +EV tick (simulated entry point)
        entry_tick = None
        peak_ev = 0.0
        peak_p = 0.0

        for tick in ticks:
            ev_up = tick.get("ev_up", 0.0)
            ev_down = tick.get("ev_down", 0.0)
            p_up = tick.get("p_up", 0.0)
            p_down = tick.get("p_down", 0.0)
            best_ev = max(ev_up, ev_down)
            best_side = "up" if ev_up >= ev_down else "down"
            best_p = p_up if best_side == "up" else p_down

            if best_ev > peak_ev:
                peak_ev = best_ev
                peak_p = best_p

            if entry_tick is None and best_ev > 0:
                entry_tick = tick
                entry_ev = best_ev
                entry_p = best_p
                entry_side = best_side

        if entry_tick is None:
            # No +EV found — record as skipped market
            market_entries.append({
                "slug": slug, "coin": coin, "interval": interval,
                "won": won, "had_positive_ev": False,
                "entry_ev": 0.0, "entry_p": 0.0, "entry_side": None,
                "peak_ev": peak_ev, "peak_p": peak_p,
                "signal_dir": ticks[-1].get("signal_dir", "flat"),
            })
            continue

        market_entries.append({
            "slug": slug, "coin": coin, "interval": interval,
            "won": won, "had_positive_ev": True,
            "entry_ev": entry_ev, "entry_p": entry_p, "entry_side": entry_side,
            "peak_ev": peak_ev, "peak_p": peak_p,
            "signal_dir": entry_tick.get("signal_dir", "flat"),
        })

    if not market_entries:
        W("\n  No markets with trade outcomes to analyze.\n\n")
        sys.stdout.flush()
        return

    with_ev = [m for m in market_entries if m["had_positive_ev"]]
    without_ev = [m for m in market_entries if not m["had_positive_ev"]]

    # ══════════════════════════════════════════════════════════════
    W(f"\n{'='*70}\n")
    W("  TICK ANALYSIS (EV vs actual outcome)\n")
    W(f"{'='*70}\n")

    # ── EV Calibration: does higher EV → higher win rate? ──
    W("\n  EV CALIBRATION\n")
    W(f"  {'-'*55}\n")
    W(f"  {'EV Range':<12} {'Markets':>7} {'Won':>5} {'Actual WR':>9}"
      f" {'Avg P(win)':>10} {'Gap':>7}\n")

    by_ev_bucket = defaultdict(list)
    for m in with_ev:
        bucket = _ev_bucket_label(m["entry_ev"])
        by_ev_bucket[bucket].append(m)

    for bucket in EV_BUCKET_ORDER:
        entries = by_ev_bucket.get(bucket, [])
        if not entries:
            continue
        won = sum(1 for m in entries if m["won"])
        actual_wr = won / len(entries) if entries else 0
        avg_p = sum(m["entry_p"] for m in entries) / len(entries)
        gap = actual_wr - avg_p
        gap_str = f"{gap:+.1%}"
        W(f"  {bucket:<12} {len(entries):>7} {won:>5} {actual_wr:>8.1%}"
          f" {avg_p:>9.1%} {gap_str:>7}\n")

    # Total row
    if with_ev:
        total_won = sum(1 for m in with_ev if m["won"])
        total_wr = total_won / len(with_ev)
        total_avg_p = sum(m["entry_p"] for m in with_ev) / len(with_ev)
        total_gap = total_wr - total_avg_p
        W(f"  {'TOTAL':<12} {len(with_ev):>7} {total_won:>5} {total_wr:>8.1%}"
          f" {total_avg_p:>9.1%} {total_gap:+.1%}\n")

    # ── Peak EV calibration (best moment in window) ──
    W("\n  PEAK EV CALIBRATION (best EV moment per market)\n")
    W(f"  {'-'*55}\n")
    W(f"  {'EV Range':<12} {'Markets':>7} {'Won':>5} {'Actual WR':>9}"
      f" {'Avg P(win)':>10} {'Gap':>7}\n")

    by_peak_bucket = defaultdict(list)
    for m in with_ev:
        bucket = _ev_bucket_label(m["peak_ev"])
        by_peak_bucket[bucket].append(m)

    for bucket in EV_BUCKET_ORDER:
        entries = by_peak_bucket.get(bucket, [])
        if not entries:
            continue
        won = sum(1 for m in entries if m["won"])
        actual_wr = won / len(entries)
        avg_p = sum(m["peak_p"] for m in entries) / len(entries)
        gap = actual_wr - avg_p
        W(f"  {bucket:<12} {len(entries):>7} {won:>5} {actual_wr:>8.1%}"
          f" {avg_p:>9.1%} {gap:+.1%}\n")

    # ── Per coin calibration ──
    W("\n  PER COIN (markets with +EV)\n")
    W(f"  {'-'*55}\n")
    W(f"  {'Coin':<6} {'Markets':>7} {'Won':>5} {'WR':>6} {'Avg EV':>8}"
      f" {'Avg P':>7} {'Gap':>7}\n")

    by_coin = defaultdict(list)
    for m in with_ev:
        by_coin[m["coin"]].append(m)

    for coin in sorted(by_coin):
        entries = by_coin[coin]
        won = sum(1 for m in entries if m["won"])
        wr = won / len(entries)
        avg_ev = sum(m["entry_ev"] for m in entries) / len(entries)
        avg_p = sum(m["entry_p"] for m in entries) / len(entries)
        gap = wr - avg_p
        W(f"  {coin.upper():<6} {len(entries):>7} {won:>5} {_pct(won, len(entries)):>6}"
          f" {avg_ev:>+7.4f} {avg_p:>6.1%} {gap:>+6.1%}\n")

    # ── Per interval calibration ──
    W("\n  PER INTERVAL (markets with +EV)\n")
    W(f"  {'-'*55}\n")
    W(f"  {'Int':<6} {'Markets':>7} {'Won':>5} {'WR':>6} {'Avg EV':>8}"
      f" {'Avg P':>7} {'Gap':>7}\n")

    by_interval = defaultdict(list)
    for m in with_ev:
        by_interval[m["interval"]].append(m)

    for iv in sorted(by_interval):
        entries = by_interval[iv]
        won = sum(1 for m in entries if m["won"])
        wr = won / len(entries)
        avg_ev = sum(m["entry_ev"] for m in entries) / len(entries)
        avg_p = sum(m["entry_p"] for m in entries) / len(entries)
        gap = wr - avg_p
        W(f"  {iv:<6} {len(entries):>7} {won:>5} {_pct(won, len(entries)):>6}"
          f" {avg_ev:>+7.4f} {avg_p:>6.1%} {gap:>+6.1%}\n")

    # ── Per coin + interval ──
    W("\n  PER COIN + INTERVAL (markets with +EV)\n")
    W(f"  {'-'*55}\n")
    W(f"  {'Coin':<6} {'Int':<4} {'Mkts':>5} {'Won':>4} {'WR':>6}"
      f" {'AvgEV':>7} {'AvgP':>6} {'Gap':>6}\n")

    by_combo = defaultdict(list)
    for m in with_ev:
        by_combo[(m["coin"], m["interval"])].append(m)

    for (coin, iv) in sorted(by_combo):
        entries = by_combo[(coin, iv)]
        won = sum(1 for m in entries if m["won"])
        wr = won / len(entries)
        avg_ev = sum(m["entry_ev"] for m in entries) / len(entries)
        avg_p = sum(m["entry_p"] for m in entries) / len(entries)
        gap = wr - avg_p
        W(f"  {coin.upper():<6} {iv:<4} {len(entries):>5} {won:>4}"
          f" {_pct(won, len(entries)):>6} {avg_ev:>+6.4f} {avg_p:>5.1%} {gap:>+5.1%}\n")

    # ── Signal direction accuracy ──
    W("\n  SIGNAL vs OUTCOME\n")
    W(f"  {'-'*55}\n")
    W(f"  {'Signal':<8} {'Markets':>7} {'Won':>5} {'WR':>6}\n")

    by_signal = defaultdict(list)
    for m in with_ev:
        by_signal[m["signal_dir"]].append(m)

    for sig in ["up", "down", "flat"]:
        entries = by_signal.get(sig, [])
        if not entries:
            continue
        won = sum(1 for m in entries if m["won"])
        W(f"  {sig:<8} {len(entries):>7} {won:>5} {_pct(won, len(entries)):>6}\n")

    # ── Skipped markets: what happened when we had no +EV ──
    if without_ev:
        skip_won = sum(1 for m in without_ev if m["won"])
        skip_lost = len(without_ev) - skip_won
        W("\n  SKIPPED MARKETS (no +EV found)\n")
        W(f"  {'-'*55}\n")
        W(f"  Total:              {len(without_ev):>5}\n")
        W(f"  Would have won:     {skip_won:>5}  ({_pct(skip_won, len(without_ev))})\n")
        W(f"  Would have lost:    {skip_lost:>5}  ({_pct(skip_lost, len(without_ev))})\n")

        # Skipped breakdown by coin
        skip_by_coin = defaultdict(lambda: {"won": 0, "lost": 0})
        for m in without_ev:
            if m["won"]:
                skip_by_coin[m["coin"]]["won"] += 1
            else:
                skip_by_coin[m["coin"]]["lost"] += 1

        W(f"\n  {'Coin':<6} {'Skipped':>7} {'Won':>5} {'Lost':>5}\n")
        for coin in sorted(skip_by_coin):
            d = skip_by_coin[coin]
            W(f"  {coin.upper():<6} {d['won']+d['lost']:>7} {d['won']:>5} {d['lost']:>5}\n")

    W("\n")
    sys.stdout.flush()
