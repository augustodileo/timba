"""Backtest: full simulation from source ticks → EVs → trades.

Reads ticks from a source env's SQLite DBs, copies them into an isolated
backtest DB, then runs the same evaluate/on_bet/resolve/record_trade
functions as the live bot. Everything stays in SQLite with full FK integrity.
"""

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from timba.backtest.common import (
    mock_market_from_tick,
    resolve_from_ticks,
    tick_data_from_dict,
)
from timba.base import RESOLVE_MAP, PositionState
from timba.state import State, init_trade_ids
from timba.strategies import get as get_strategy
from timba.strategies import load_strategies
from timba.ticks import init_ids, write_strategy_data


def backtest_main(
    config, bt_dir: Path, source_env: str, strategy: str = "favorite",
    since: str | None = None,
) -> None:
    """Run a full backtest: copy ticks from source env, evaluate, simulate trades.

    Creates data/{current-env}/backtest/bot.db with ticks, EVs, and trades.
    since: ISO date string (e.g. '2026-03-30') to filter ticks.
    """
    from timba import db

    load_strategies()
    strat = get_strategy(strategy)
    if strat is None:
        print(f"Error: strategy '{strategy}' not found.", file=sys.stderr)
        sys.exit(1)

    scfg = config.get_strategy(strategy)

    # ── Locate source ticks ──
    source_dir = Path("data") / source_env
    source_dbs = db.list_source_databases(source_dir)
    if not source_dbs:
        print(f"Error: no databases found in {source_dir}", file=sys.stderr)
        sys.exit(1)

    # ── Fresh backtest DB ──
    bt_dir.mkdir(parents=True, exist_ok=True)
    bot_db = bt_dir / "bot.db"
    if bot_db.exists():
        bot_db.unlink()
        # Clean WAL/SHM files
        for suffix in ("-wal", "-shm"):
            f = Path(str(bot_db) + suffix)
            if f.exists():
                f.unlink()

    db.init(bt_dir)

    # ── Copy ticks from source ──
    W = sys.stdout.write
    since_label = f" since={since}" if since else ""
    W(f"\nBacktest ({strategy}): source={source_env}{since_label}\n")
    total_ticks = db.copy_ticks_from(source_dbs, since=since)
    W(f"  Copied {total_ticks:,} ticks from {len(source_dbs)} source DBs\n")

    if total_ticks == 0:
        print("Error: no ticks found in source databases.", file=sys.stderr)
        db.reset()
        sys.exit(1)

    # Seed ID counters from backtest DB
    init_ids()
    init_trade_ids()

    # ── Load ticks from backtest DB ──
    ticks_by_slug, _ = db.load_ticks_grouped()
    W(f"  {sum(len(v) for v in ticks_by_slug.values()):,} ticks across {len(ticks_by_slug)} markets\n")

    # ── State for trade recording ──
    state = State()

    # ── Market configs lookup ──
    market_cfgs = {}
    for m in scfg.markets:
        market_cfgs[(m["coin"], m["interval"])] = m

    scfg.get("contracts_per_trade") or 5  # 0 invalid per schema

    # ── Simulation loop ──
    results = []

    for slug, ticks in ticks_by_slug.items():
        coin = ticks[0]["coin"]
        interval = ticks[0]["interval"]
        mcfg = market_cfgs.get((coin, interval))
        if mcfg is None:
            continue

        # Fresh position per slug — same as live _register_for_strategy
        mock_market = mock_market_from_tick(ticks[0])
        pos = strat.create_position(mock_market, mcfg, scfg)
        if pos is None:
            continue
        pos.data_dir = str(bt_dir)
        pos.market_mode = "paper"

        bet_placed = False

        for tick in ticks:
            tick_data = tick_data_from_dict(tick)
            if tick_data is None:
                continue

            pos.end_timestamp = mock_market.end_timestamp

            # Same as live: strategy.evaluate()
            decision = strat.evaluate(pos, tick_data)
            pos.skip_reason = decision.reason

            # Write EV to SQLite — same as live
            if decision.computed:
                ev_id = write_strategy_data(
                    None, strategy, "evs", decision.computed,
                    slug=slug, tick_id=tick_data.tick_id,
                )
                pos.ev_id = ev_id

            # Paper fill — replaces _execute_bet + _handle_order
            if decision.should_bet and not bet_placed:
                pos.state = PositionState.PAPER
                pos.side = decision.side
                pos.buy_price = decision.price
                pos.contracts = decision.size
                pos.cost = decision.price * decision.size
                pos.sniped_at = tick.get("ts", datetime.now(timezone.utc).isoformat())
                strat.on_bet(pos, decision)
                bet_placed = True

        # Timeout — if never bet, mark as skipped
        if pos.state == PositionState.WATCHING:
            pos.state = PositionState.SKIPPED
            pos.sniped_at = ticks[-1].get("ts", "")
            if not pos.skip_reason:
                pos.skip_reason = "window timeout"

        # Resolve — from last tick midpoints
        winning_side = resolve_from_ticks(ticks)
        pos.resolved_at = ticks[-1].get("ts", "")

        if winning_side is None:
            if not pos.side:
                pos.state = PositionState.SKIP_NONE
            won = False
        else:
            won = pos.side == winning_side if pos.side else False

        # Same as live: strat.resolve() + RESOLVE_MAP + record_trade
        mapping = RESOLVE_MAP.get(pos.state)
        if mapping is not None:
            strat.resolve(pos, won)

            if not pos.side and pos.state == PositionState.SKIPPED:
                pos.state = PositionState.SKIP_NONE
            else:
                _, state_won, state_lost = mapping
                pos.state = state_won if won else state_lost

            state.record_trade(pos, strategy, extra_fields=strat.extra_fields(pos))

        results.append({
            "slug": slug, "coin": coin, "interval": interval,
            "entered": bet_placed,
            "won": won if winning_side else None,
            "pnl": pos.pnl,
            "side": pos.side,
            "winning_side": winning_side,
            "type": pos.state.value,
        })

    # ── Flush all writes ──
    db.flush()

    # ── Summary report ──
    entered = [r for r in results if r["entered"]]
    with_outcome = [r for r in entered if r["won"] is not None]
    wins = [r for r in with_outcome if r["won"]]
    losses = [r for r in with_outcome if not r["won"]]

    W(f"\n{'='*70}\n")
    W(f"  BACKTEST TRADES ({strategy.upper()})\n")
    W(f"{'='*70}\n\n")
    W(f"  Markets analyzed: {len(results):>5}\n")
    W(f"  Would enter:      {len(entered):>5}\n")
    W(f"  With outcome:     {len(with_outcome):>5}\n")

    if with_outcome:
        wr = len(wins) / len(with_outcome)
        total_pnl = sum(r["pnl"] for r in with_outcome)
        pnl_per = total_pnl / len(with_outcome)
        W(f"\n  Wins:  {len(wins):>5}   Losses: {len(losses):>5}   WR: {wr:.1%}\n")
        W(f"  PnL:   ${total_pnl:+.2f}  (${pnl_per:+.3f}/trade)\n")

    # Per coin x interval
    combos = defaultdict(list)
    for r in results:
        combos[(r["coin"], r["interval"])].append(r)

    W(f"\n  {'Coin':<6} {'Int':<4} {'Mkts':>5} {'Enter':>5} {'W':>4} {'L':>4}"
      f" {'WR':>6} {'PnL':>9}\n")
    W("  " + "-" * 50 + "\n")

    for (coin, iv) in sorted(combos.keys()):
        group = combos[(coin, iv)]
        ge = [r for r in group if r["entered"]]
        go = [r for r in ge if r["won"] is not None]
        gw = sum(1 for r in go if r["won"])
        gl = len(go) - gw
        gwr = gw / len(go) if go else 0
        gpnl = sum(r["pnl"] for r in go)

        W(f"  {coin.upper():<6} {iv:<4} {len(group):>5} {len(ge):>5} {gw:>4} {gl:>4}"
          f" {gwr:>5.1%} {gpnl:>+8.2f}\n")

    W(f"\n  Results in: {bt_dir}/bot.db\n")
    W(f"  Analyze: timba --analyze-trades --backtest --strategy {strategy}\n")
    W("  Clean:   timba --backtest-clean\n")
    W("\n")
    sys.stdout.flush()

    db.reset()
