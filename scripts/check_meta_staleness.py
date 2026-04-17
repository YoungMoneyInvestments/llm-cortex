#!/usr/bin/env python3
"""
FB/IG Messaging Staleness Monitor
===================================
Checks MAX(sent_at) for facebook and instagram rows in messaging_messages
on the Storage VPS. If either platform is stale by > STALE_DAYS (default 14),
writes an alert observation to the cortex memory worker.

Designed to be called from the weekly maintenance cron (com.cortex.maintenance)
or standalone. Additive -- does NOT modify maintenance.py.

Usage:
    python scripts/check_meta_staleness.py [--stale-days N] [--dry-run]

Exit codes:
    0 -- check ran (stale or not; alert written if needed)
    1 -- hard failure (DB unreachable, missing deps)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("meta-staleness")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PG_HOST = os.environ.get("CORTEX_PG_HOST", "100.67.112.3")
PG_PORT = int(os.environ.get("CORTEX_PG_PORT", "5432"))
PG_DB   = os.environ.get("CORTEX_PG_DB", "tradingcore")
PG_USER = os.environ.get("CORTEX_PG_USER", "trading_user")
PG_PASS = os.environ.get("CORTEX_PG_PASSWORD", "YS6X7G!aBLQw*Pzd#JUiboz^")

CORTEX_WORKER_URL = "http://localhost:37778"
_KEY_FILE = Path.home() / ".cortex" / "data" / ".worker_api_key"

PLATFORMS = ("facebook", "instagram")
DEFAULT_STALE_DAYS = 14

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_worker_api_key() -> str:
    """Read cortex worker API key from env, key file, or launchd process env."""
    env_key = os.environ.get("CORTEX_WORKER_API_KEY", "").strip()
    if env_key:
        return env_key
    if _KEY_FILE.exists():
        key = _KEY_FILE.read_text().strip()
        if key:
            return key
    # Fallback: probe live process env (same technique as smoke_test.py)
    try:
        import subprocess
        result = subprocess.run(["ps", "eww", "-ax"], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if "memory_worker" in line and "CORTEX_WORKER_API_KEY=" in line:
                idx = line.index("CORTEX_WORKER_API_KEY=")
                rest = line[idx + len("CORTEX_WORKER_API_KEY="):]
                candidate = rest.split()[0]
                if candidate:
                    return candidate
    except Exception:
        pass
    return ""


def _query_max_sent_at() -> dict[str, datetime | None]:
    """
    Query the Storage VPS for MAX(sent_at) per platform.

    Returns {platform: datetime_or_None, ...} for PLATFORMS.
    Raises on connection failure.
    """
    import psycopg2  # type: ignore

    conn = psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DB,
        user=PG_USER,
        password=PG_PASS,
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            # platform column is a custom enum (messaging_platform), so cast
            cur.execute(
                """
                SELECT platform::text, MAX(sent_at) AS last_sent
                FROM messaging_messages
                WHERE platform::text = ANY(%s)
                GROUP BY platform
                """,
                (list(PLATFORMS),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    result: dict[str, datetime | None] = {p: None for p in PLATFORMS}
    for platform, last_sent in rows:
        if platform in result:
            result[platform] = last_sent
    return result


def _save_alert_to_cortex(content: str, tags: str, dry_run: bool) -> bool:
    """
    POST an alert to the cortex memory worker via /api/memory/save.

    Returns True on success, False on failure.
    """
    if dry_run:
        log.info("[dry-run] Would save alert: %s", content[:120])
        return True

    api_key = _get_worker_api_key()
    if not api_key:
        log.warning("No cortex worker API key found -- writing alert to stderr only")
        print(f"ALERT (no cortex key): {content}", file=sys.stderr)
        return False

    payload = json.dumps({"content": content, "tags": tags}).encode("utf-8")
    req = urllib.request.Request(
        f"{CORTEX_WORKER_URL}/api/memory/save",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Cortex-Api-Key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            log.info("Alert saved to cortex: id=%s", body.get("id", "?"))
            return True
    except urllib.error.HTTPError as exc:
        log.error("Cortex worker returned HTTP %d: %s", exc.code, exc.reason)
        print(f"ALERT (cortex error {exc.code}): {content}", file=sys.stderr)
        return False
    except Exception as exc:
        log.error("Could not reach cortex worker: %s", exc)
        print(f"ALERT (cortex unreachable): {content}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FB/IG messaging staleness monitor")
    parser.add_argument(
        "--stale-days",
        type=int,
        default=DEFAULT_STALE_DAYS,
        help=f"Days without new messages before alerting (default: {DEFAULT_STALE_DAYS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check staleness but do not write cortex alert",
    )
    args = parser.parse_args(argv)

    log.info("=== Meta (FB/IG) staleness check START ===")
    log.info("stale_threshold=%d days  dry_run=%s", args.stale_days, args.dry_run)

    # Query the VPS
    try:
        max_sent = _query_max_sent_at()
    except Exception as exc:
        log.error("Cannot connect to Storage VPS: %s", exc)
        return 1

    now_utc = datetime.now(timezone.utc)
    stale_platforms: list[str] = []
    alert_lines: list[str] = []

    for platform in PLATFORMS:
        last = max_sent.get(platform)
        if last is None:
            age_days = None
            status = "NO DATA"
            is_stale = True
        else:
            # Ensure timezone-aware for subtraction
            if last.tzinfo is None:
                from datetime import timezone as tz
                last = last.replace(tzinfo=tz.utc)
            age_days = (now_utc - last).days
            is_stale = age_days > args.stale_days
            status = f"STALE ({age_days}d)" if is_stale else f"ok ({age_days}d)"

        log.info("  %s: last_sent=%s  status=%s", platform, last, status)

        if is_stale:
            stale_platforms.append(platform)
            days_str = f"{age_days} days" if age_days is not None else "no data"
            alert_lines.append(
                f"  - {platform}: last message {days_str} ago"
                f" (last_sent={last.date() if last else 'N/A'})"
            )

    if not stale_platforms:
        log.info("All platforms fresh. No alert needed.")
        log.info("=== Meta staleness check COMPLETE (ok) ===")
        return 0

    # Build alert content
    platforms_str = " + ".join(stale_platforms)
    alert_content = (
        f"[ALERT] FB/IG messaging data stale: {platforms_str}\n"
        + "\n".join(alert_lines)
        + f"\n\nThreshold: {args.stale_days} days."
        " Re-run ~/Projects/messaging-migration scripts after downloading"
        " a fresh Meta data export from https://accountscenter.facebook.com/"
        " (Your information and permissions -> Download your information -> JSON format)."
        f"\n\nChecked: {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )

    log.warning("STALE PLATFORMS: %s", stale_platforms)
    for line in alert_lines:
        log.warning(line)

    saved = _save_alert_to_cortex(
        content=alert_content,
        tags="alert,meta,facebook,instagram,messaging,staleness",
        dry_run=args.dry_run,
    )

    if not saved and not args.dry_run:
        log.error("Alert could not be written to cortex (see stderr)")

    log.info("=== Meta staleness check COMPLETE (stale) ===")
    return 0  # Exit 0 even when stale -- the alert was written; don't break maintenance


if __name__ == "__main__":
    sys.exit(main())
