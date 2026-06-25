#!/usr/bin/env python3
"""
Contract Roll Monitor
=====================
Detects stale market data in core.market_data (bar_size='1 min')
that likely indicates a missed contract roll.

Checks:
  1. Per-symbol data age - warns if latest bar > STALE_DAYS old
  2. Upcoming/overdue rolls from ROLL_SCHEDULE - warns within 7 days

Run weekly via cron (Monday 8:05 AM CT) or on-demand.

Exit codes: 0 = all OK, 1 = stale symbols detected
"""

import os
import sys
import json
import logging
import psycopg2
from datetime import date, datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("roll_monitor")

# -- Config -------------------------------------------------------------------
DB_HOST = os.getenv("POSTGRES_HOST", "100.67.112.3")
DB_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
DB_NAME = os.getenv("POSTGRES_DB", "tradingcore")
DB_USER = os.getenv("POSTGRES_USER", "trading_user")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")

DISCORD_WEBHOOK = os.getenv("KPL_DISCORD_WEBHOOK", "")

STALE_DAYS = 4  # Alert if latest bar is older than this many days

# fmt: off
# Roll schedule: symbol -> (contract, expiry_date)
# Update after each roll. Next known rolls listed as comments.
ROLL_SCHEDULE = {
    # Equity indices - quarterly (next roll ~Sep 18 2026)
    "ES":  ("ES 06-26",  date(2026, 6, 20)),
    "NQ":  ("NQ 06-26",  date(2026, 6, 20)),
    "YM":  ("YM 06-26",  date(2026, 6, 20)),
    "RTY": ("RTY 06-26", date(2026, 6, 20)),
    "MES": ("MES 06-26", date(2026, 6, 20)),
    "MNQ": ("MNQ 06-26", date(2026, 6, 20)),
    "MYM": ("MYM 06-26", date(2026, 6, 20)),
    "M2K": ("M2K 06-26", date(2026, 6, 20)),
    # Energies - monthly
    "CL":  ("CL 07-26",  date(2026, 6, 20)),   # July CL expires ~Jun 20
    "MCL": ("MCL 07-26", date(2026, 6, 20)),
    "NG":  ("NG 07-26",  date(2026, 6, 26)),    # July NG expires ~Jun 26
    # Metals - bi-monthly
    "GC":  ("GC 08-26",  date(2026, 7, 29)),
    "MGC": ("MGC 08-26", date(2026, 7, 29)),
    "SI":  ("SI 07-26",  date(2026, 6, 27)),
    "HG":  ("HG 07-26",  date(2026, 6, 27)),
    # Grains - seasonal
    "ZC":  ("ZC 07-26",  date(2026, 7, 14)),
    "ZW":  ("ZW 07-26",  date(2026, 7, 14)),
    # Treasuries / FX - quarterly
    "ZN":  ("ZN 06-26",  date(2026, 6, 19)),
    "ZB":  ("ZB 06-26",  date(2026, 6, 19)),
    "6E":  ("6E 06-26",  date(2026, 6, 19)),
    # Micros tracked in smart_backfill but not KPL
    "MBT": ("MBT 06-26", date(2026, 6, 20)),
}
# fmt: on

TRACKED_SYMBOLS = list(ROLL_SCHEDULE.keys())


def get_db():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS,
        connect_timeout=15,
    )


def check_db_freshness() -> dict:
    """Return {symbol: latest_bar_datetime} for all tracked symbols."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, MAX(time) AS latest
                FROM core.market_data
                WHERE symbol = ANY(%s)
                  AND bar_size = '1 min'
                GROUP BY symbol
                """,
                (TRACKED_SYMBOLS,),
            )
            rows = cur.fetchall()
    return {r[0]: r[1] for r in rows}


def post_discord(message: str) -> bool:
    if not DISCORD_WEBHOOK:
        return True
    try:
        import urllib.request
        data = json.dumps({"content": message}).encode()
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as exc:
        log.error("Discord post failed: %s", exc)
        return False


def main() -> int:
    today = date.today()
    now = datetime.now(timezone.utc)
    log.info("=== Contract Roll Monitor - %s ===", today)

    freshness = check_db_freshness()
    alerts = []
    warn_count = 0

    for sym in TRACKED_SYMBOLS:
        contract, expiry = ROLL_SCHEDULE[sym]
        days_to_expiry = (expiry - today).days

        # Check DB staleness
        if sym not in freshness:
            msg = f"NO DATA: {sym} ({contract}) - no 1-min bars in DB at all"
            log.warning(msg)
            alerts.append(msg)
            warn_count += 1
            continue

        latest = freshness[sym]
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        age_days = (now - latest).total_seconds() / 86400

        if age_days > STALE_DAYS:
            msg = (
                f"STALE: {sym} ({contract}) - last bar {latest.date()} "
                f"({age_days:.1f} days ago)"
            )
            log.warning(msg)
            alerts.append(msg)
            warn_count += 1
        else:
            log.info("OK: %s (%s) latest=%s", sym, contract, latest.date())

        # Warn on upcoming roll (within 7 days)
        if 0 <= days_to_expiry <= 7:
            msg = f"ROLL SOON: {sym} contract {contract} expires {expiry} ({days_to_expiry} days)"
            log.warning(msg)
            alerts.append(msg)
        elif days_to_expiry < 0:
            msg = f"ROLL OVERDUE: {sym} contract {contract} expired {expiry} ({-days_to_expiry} days ago)"
            log.warning(msg)
            alerts.append(msg)

    log.info("=== Done: %d issue(s) found ===", len(alerts))

    if alerts:
        summary = (
            f"**Contract Roll Monitor - {today}**\n"
            + "\n".join(f"- {a}" for a in alerts)
            + "\n\nUpdate `CONTRACT_MAP` in `smart_backfill.py` and `ROLL_SCHEDULE` in `contract_roll_monitor.py`."
        )
        discord_ok = post_discord(summary)
        for a in alerts:
            print(a, file=sys.stderr)
        if DISCORD_WEBHOOK and not discord_ok:
            print("ALERT DELIVERY FAILED: Discord webhook post failed", file=sys.stderr)
            return 2

    return 1 if warn_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
