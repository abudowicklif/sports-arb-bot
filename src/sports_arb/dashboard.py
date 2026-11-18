"""Static HTML dashboard built only from recorded state. Panels that need a component that
does not exist (sports feed, paper trading, signal log) say NOT CONNECTED instead of faking data."""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import time
from pathlib import Path

from sports_arb.accounting import CapitalAccount
from sports_arb.paths import DATA_DIR
from sports_arb.quant.registry import STRATEGIES, ValidationRegistry

STYLE = (
    "body{font-family:sans-serif;margin:1.5rem}table{border-collapse:collapse}"
    "td{border:1px solid #ccc;padding:.25rem .6rem}.bad{color:#b00020;font-weight:bold}"
    "section{margin-bottom:1.2rem}"
)


def _table(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>" for k, v in rows
    )
    return f"<table>{body}</table>"


def _panel(title: str, content: str) -> str:
    return f"<section><h2>{html.escape(title)}</h2>{content}</section>"


def _not_connected(what: str) -> str:
    return f"<p class='bad'>NOT CONNECTED: {html.escape(what)}</p>"


def render_dashboard(data_dir: Path = DATA_DIR, now: float | None = None) -> str:
    now = time.time() if now is None else now
    panels: list[str] = []

    status_path = data_dir / "service_status.json"
    if status_path.exists():
        st = json.loads(status_path.read_text())
        age = now - st["ts"]
        rows = [
            ("overall", st["overall"] if age < 30 else f"STALE ({age:.0f}s old)"),
            ("trading_allowed", st["trading_allowed"] and age < 30),
        ]
        rows += [
            (f"component {n}", f"{c['state']} (restarts {c['restarts']}) {c['last_error']}")
            for n, c in st["components"].items()
        ]
        panels.append(_panel("System health", _table(rows)))
    else:
        panels.append(_panel("System health", _not_connected("supervisor has not run")))

    capital_db = data_dir / "capital.db"
    if capital_db.exists():
        acct = CapitalAccount(capital_db)
        snap = acct.snapshot()
        snap["loss_limit_hit"] = str(acct.loss_limit_hit())
        panels.append(_panel("Capital and profit allocation", _table(list(snap.items()))))
        by = acct.pnl_by_strategy()
        rows = [
            (s, f"{len(by.get(s, []))} trades, net {sum(by.get(s, []), 0)}") for s in STRATEGIES
        ]
        panels.append(_panel("Realized performance by strategy", _table(rows)))
    else:
        panels.append(_panel("Capital", _not_connected("no capital ledger yet")))

    reg = ValidationRegistry(data_dir / "validation_registry.json")
    panels.append(
        _panel("Strategy validation status", _table([(s, reg.status(s)) for s in STRATEGIES]))
    )

    live_db = data_dir / "live.db"
    if live_db.exists():
        db = sqlite3.connect(live_db)
        rows = db.execute(
            "SELECT venue, status, COUNT(*) FROM rounds GROUP BY venue, status ORDER BY 1, 2"
        ).fetchall()
        panels.append(
            _panel(
                "Execution rounds (live ledger)",
                _table([(f"{v}/{s}", n) for v, s, n in rows])
                if rows
                else "<p>No rounds recorded.</p>",
            )
        )
    else:
        panels.append(_panel("Execution rounds", "<p>No live ledger: no real orders placed.</p>"))

    alerts_path = data_dir / "alerts.jsonl"
    alerts = alerts_path.read_text().splitlines()[-15:] if alerts_path.exists() else []
    panels.append(
        _panel(
            "Recent alerts",
            _table(
                [
                    (
                        f"{json.loads(a)['severity']} {json.loads(a)['component']}",
                        json.loads(a)["message"],
                    )
                    for a in alerts
                ]
            )
            if alerts
            else "<p>None.</p>",
        )
    )

    unavailable = (
        ("Sports-data connectivity", "no licensed sports feed integrated"),
        ("Live sports being monitored", "needs sports feed + match tracker wiring"),
        ("Opportunities detected / rejected", "decision engine not wired to a feed"),
        ("Paper trading", "no paper-trading run recorded"),
        ("Unrealized P&L / open positions", "no position reconciliation wired"),
    )
    panels.extend(_panel(title, _not_connected(what)) for title, what in unavailable)

    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='10'><title>sports-arb dashboard</title>"
        f"<style>{STYLE}</style></head><body><h1>sports-arb dashboard</h1>"
        f"<p>Generated {stamp} UTC. Figures come only from recorded transactions.</p>"
        f"{''.join(panels)}</body></html>"
    )


def main() -> None:
    p = argparse.ArgumentParser(prog="sports-arb-dashboard")
    p.add_argument("--data", type=Path, default=DATA_DIR)
    p.add_argument("--out", type=Path, default=DATA_DIR / "dashboard.html")
    a = p.parse_args()
    a.out.write_text(render_dashboard(a.data))
    print(a.out)


if __name__ == "__main__":
    main()
