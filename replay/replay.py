"""
replay/replay.py — run the EXACT strategy code over recorded ticks.

Feeds ticks from ticks/ticks_YYYYMMDD.jsonl.gz through buy_app.process_ticks
with:
  * a simulated clock  (core.clock.set_clock)   — timeouts, SL phases and
    session gates behave identically to live
  * a NullEmitter      (core.emitter)           — no SocketIO, no web server
  * the PaperBroker    (core.broker)            — instant fills + fill ledger
  * isolated state dirs                          — replay never pollutes live
    brain/analyzer/intel state or the live trade log

ATM option tokens come from recorded `atm_resolved` meta events instead of
REST calls (thread spawns are disabled via S["_replay"]).

CLI:
  python -m replay.replay --date 20260708
  python -m replay.replay --file ticks/ticks_20260708.jsonl.gz --config overrides.json
"""

import argparse
import gzip
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_tick_file(path: str) -> list[dict]:
    lines = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                lines.append(json.loads(raw))
    lines.sort(key=lambda x: x.get("t", 0))
    return lines


class ReplayRunner:

    def __init__(self, files: list[str], *, config_overrides: dict | None = None,
                 slippage_bps: float = 0.0, fee_per_order: float = 0.0,
                 out_dir: str | None = None, capital: float | None = None,
                 batch_secs: float = 0.05):
        self.files      = files
        self.overrides  = config_overrides or {}
        self.slippage   = slippage_bps
        self.fee        = fee_per_order
        self.batch_secs = batch_secs
        self.capital    = capital
        run_id = datetime.now().strftime("%H%M%S")
        self.out_dir = out_dir or os.path.join(_BASE_DIR, "replay_out",
                                               f"run_{run_id}")

    # ── environment isolation ────────────────────────────────────────────
    def _isolate(self):
        os.makedirs(self.out_dir, exist_ok=True)

        import buy_exit_strategy as bes
        import exit_analyzer, market_brain, entry_analyzer, market_intelligence
        import position_sizer
        import excel_logger
        bes.LOG_DIR                  = self.out_dir
        exit_analyzer.STATE_DIR      = self.out_dir
        market_brain.STATE_DIR       = self.out_dir
        entry_analyzer.STATE_DIR     = self.out_dir
        market_intelligence.STATE_DIR = self.out_dir
        position_sizer.STATE_DIR     = self.out_dir
        excel_logger.EXCEL_LOG       = os.path.join(self.out_dir, "trade_log.xlsx")

        from core import emitter
        emitter.set_emitter(emitter.NullEmitter())

        from core.broker import reset_paper_broker
        self.broker = reset_paper_broker(self.slippage, self.fee)

    # ── main ─────────────────────────────────────────────────────────────
    def run(self) -> dict:
        self._isolate()

        # Import AFTER isolation so module-level engines (MarketBrain etc.)
        # load state from the replay dir, not the live one.
        import buy_app
        from core import clock

        S, RC = buy_app.S, buy_app.RC

        # Config overrides (A/B a parameter change against the same day)
        for k, v in self.overrides.items():
            if k in RC:
                RC[k] = v
            else:
                print(f"[replay] override ignored (not an RC key): {k}")

        lines = []
        for f in self.files:
            lines.extend(load_tick_file(f))
        lines.sort(key=lambda x: x.get("t", 0))
        if not lines:
            raise SystemExit("no ticks found in input files")

        # Simulated clock: naive IST derived from the recorded epoch stamps
        sim_t = {"now": lines[0]["t"]}
        clock.set_clock(lambda: datetime.fromtimestamp(sim_t["now"], clock.IST)
                        .replace(tzinfo=None))

        try:
            with buy_app._state_lock:
                buy_app._reset_session_state(buy_app.NIFTY_TOKEN, "NIFTY")
                S["_replay"]      = True
                S["trading_mode"] = "demo"
                if self.capital:
                    S["capital"] = S["day_start_capital"] = self.capital
                    S["exit_engine"].capital = self.capital
                S["trading_date"] = (datetime.fromtimestamp(lines[0]["t"], clock.IST)
                                     .date())

            n_ticks, n_meta = 0, 0
            batch: list[dict] = []
            batch_t = lines[0]["t"]

            def flush():
                if batch:
                    buy_app.process_ticks(batch)
                    batch.clear()

            for line in lines:
                sim_t["now"] = line["t"]
                if "meta" in line:
                    flush()
                    self._apply_meta(buy_app, line)
                    n_meta += 1
                    continue
                if line["t"] - batch_t > self.batch_secs:
                    flush()
                    batch_t = line["t"]
                batch.append({
                    "instrument_token": line["tk"],
                    "last_price":       line["p"],
                    "volume_traded":    line.get("v", 0),
                })
                n_ticks += 1
            flush()

            # Close any still-open trade at end of data
            if S["trade_open"]:
                buy_app._close_active_trade(reason="force_exit")

        finally:
            clock.clear_clock()
            with buy_app._state_lock:
                S["_replay"] = False
                S["running"] = False

        from replay.report import build_report
        report = build_report(os.path.join(self.out_dir, "trade_log.csv"),
                              n_ticks=n_ticks, n_meta=n_meta,
                              fills=self.broker.fills)
        report_path = os.path.join(self.out_dir, "report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        report["out_dir"] = self.out_dir
        return report

    def _apply_meta(self, buy_app, line: dict):
        event = line.get("meta")
        if event == "atm_resolved":
            with buy_app._state_lock:
                S = buy_app.S
                if line.get("ce_token"):
                    S["slots"]["CE"]["token"]  = line["ce_token"]
                    S["slots"]["CE"]["symbol"] = line.get("ce_symbol", "")
                    S["slots"]["CE"]["strike"] = line.get("strike")
                if line.get("pe_token"):
                    S["slots"]["PE"]["token"]  = line["pe_token"]
                    S["slots"]["PE"]["symbol"] = line.get("pe_symbol", "")
                    S["slots"]["PE"]["strike"] = line.get("strike")
                S["nifty_atm"] = {
                    "strike":    line.get("strike"),
                    "expiry":    line.get("expiry"),
                    "ce_token":  line.get("ce_token"),
                    "pe_token":  line.get("pe_token"),
                    "ce_symbol": line.get("ce_symbol"),
                    "pe_symbol": line.get("pe_symbol"),
                }
                S["atm_pending"] = False
            if line.get("expiry"):
                buy_app._market_intel.update_expiry(line["expiry"])
        # session_start meta: informational (RC snapshot available in file)


def main():
    ap = argparse.ArgumentParser(description="Replay recorded ticks through the bot")
    ap.add_argument("--date", help="YYYYMMDD — replays ticks/ticks_<date>.jsonl.gz")
    ap.add_argument("--file", action="append", help="explicit tick file path(s)")
    ap.add_argument("--config", help="JSON file of RC overrides for A/B runs")
    ap.add_argument("--slippage-bps", type=float, default=0.0)
    ap.add_argument("--fee", type=float, default=0.0)
    ap.add_argument("--capital", type=float, default=None)
    args = ap.parse_args()

    files = list(args.file or [])
    if args.date:
        files.append(os.path.join(_BASE_DIR, "ticks", f"ticks_{args.date}.jsonl.gz"))
    if not files:
        ap.error("provide --date or --file")

    overrides = {}
    if args.config:
        with open(args.config, encoding="utf-8") as f:
            overrides = json.load(f)

    runner = ReplayRunner(files, config_overrides=overrides,
                          slippage_bps=args.slippage_bps, fee_per_order=args.fee,
                          capital=args.capital)
    report = runner.run()
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
