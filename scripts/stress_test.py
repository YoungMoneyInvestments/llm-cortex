#!/usr/bin/env python3
"""
Pass DD Stress Test -- Concurrent load probing for cortex memory worker.

Tests SQLite lock contention, request-queue behavior, tail latency, memory
growth, and connection leaks under load.

Usage:
    python scripts/stress_test.py                          # run all 4 scenarios
    python scripts/stress_test.py --scenario light         # single scenario
    python scripts/stress_test.py --concurrency 50 --duration 30 --endpoint search
    python scripts/stress_test.py --help

Scenarios:
    light   - 10 concurrent, 10s, /api/memory/search
    heavy   - 100 concurrent, 30s, /api/memory/search
    auth    - 100 concurrent, missing key, /api/memory/search (expect 100% 401)
    mixed   - 50 concurrent /api/memory/search + 50 /api/observations/recent

Output:
    Results saved to ~/.cortex/eval/stress_test_<timestamp>.json
    Console: per-scenario latency table + bug list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────

WORKER_URL = "http://localhost:37778"
KEY_FILE = Path.home() / ".cortex" / "data" / ".worker_api_key"
EVAL_DIR = Path.home() / ".cortex" / "eval"
LOG_FILE = Path.home() / ".openclaw" / "logs" / "memory-worker.log"

# Varied query strings -- avoids SQLite page-cache masking contention
SEARCH_QUERIES = [
    "BrokerBridge architecture",
    "trading risk management",
    "KPL key price levels",
    "matrix LSTM model",
    "PostgreSQL VPS storage",
    "session bootstrap context",
    "stop loss guardian",
    "knowledge graph entities",
    "IBKR gateway connection",
    "cortex memory worker",
    "NinjaTrader execution",
    "subscription tier rate limit",
    "vector search embeddings",
    "identity resolver Cami",
    "adversarial improvement loop",
    "market data pipeline",
    "compression AI summarizer",
    "observation retention cleanup",
    "session handoff notes",
    "feature engineering signals",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _read_api_key() -> str:
    """Return the API key the live worker is using.

    Priority:
    1. CORTEX_WORKER_API_KEY env var
    2. ps eww -- sniff from live worker process environment
    3. ~/.cortex/data/.worker_api_key file
    4. Empty string (auth tests will use this intentionally)
    """
    env_key = os.environ.get("CORTEX_WORKER_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        result = subprocess.run(
            ["ps", "eww", "-ax"],
            capture_output=True, text=True,
        )
        for line in result.stdout.splitlines():
            if "memory_worker" in line and "CORTEX_WORKER_API_KEY=" in line:
                # Use split on env var boundary, not whitespace (env vars may
                # contain spaces but keys won't)
                parts = line.split()
                for part in parts:
                    if part.startswith("CORTEX_WORKER_API_KEY="):
                        key = part[len("CORTEX_WORKER_API_KEY="):]
                        key = key.strip("'\"")  # strip shell quoting
                        if key:
                            return key
    except Exception:
        pass
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    return ""


def _get_worker_pid() -> Optional[int]:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "memory_worker"],
            capture_output=True, text=True,
        )
        pids = [int(x) for x in result.stdout.strip().split() if x.isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def _get_rss_kb(pid: int) -> Optional[int]:
    try:
        result = subprocess.run(
            ["ps", "-A", "-o", "pid,rss"],
            capture_output=True, text=True,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].strip() == str(pid):
                return int(parts[1])
    except Exception:
        pass
    return None


def _get_fd_count(pid: int) -> Optional[int]:
    try:
        result = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True, text=True,
        )
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        return max(0, len(lines) - 1)  # subtract header
    except Exception:
        return None


def _log_line_count() -> Optional[int]:
    try:
        if not LOG_FILE.exists():
            return None
        result = subprocess.run(
            ["wc", "-l", str(LOG_FILE)],
            capture_output=True, text=True,
        )
        return int(result.stdout.strip().split()[0])
    except Exception:
        return None


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = int(len(sorted_values) * p / 100)
    idx = min(idx, len(sorted_values) - 1)
    return sorted_values[idx]


# ── Request worker ───────────────────────────────────────────────────────────

async def _single_request(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: Optional[str],
    query_idx: int,
) -> dict:
    """Fire one request and return timing + result metadata."""
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    t0 = time.perf_counter()
    error_msg = None
    status_code = None
    is_valid_json = False
    has_stack_trace = False

    try:
        if endpoint == "/api/memory/search":
            query = SEARCH_QUERIES[query_idx % len(SEARCH_QUERIES)]
            resp = await client.post(
                f"{WORKER_URL}{endpoint}",
                json={"query": query, "limit": 5},
                headers=headers,
            )
        elif endpoint == "/api/observations/recent":
            resp = await client.get(
                f"{WORKER_URL}{endpoint}?limit=10",
                headers=headers,
            )
        elif endpoint == "/api/stats":
            resp = await client.get(
                f"{WORKER_URL}{endpoint}",
                headers=headers,
            )
        else:
            raise ValueError(f"Unknown endpoint: {endpoint}")

        status_code = resp.status_code
        body_text = resp.text
        try:
            json.loads(body_text)
            is_valid_json = True
        except json.JSONDecodeError:
            is_valid_json = False
            error_msg = f"Invalid JSON body: {body_text[:200]}"

        # Check for stack traces in error bodies
        if status_code >= 400 and any(
            marker in body_text
            for marker in ("Traceback", "File \"", "  File ", "raise ", "AssertionError")
        ):
            has_stack_trace = True
            error_msg = f"Stack trace in error body: {body_text[:300]}"

    except httpx.TimeoutException as exc:
        error_msg = f"Timeout: {exc}"
        status_code = -1
    except httpx.ConnectError as exc:
        error_msg = f"ConnectError: {exc}"
        status_code = -2
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        status_code = -3

    latency_ms = (time.perf_counter() - t0) * 1000
    return {
        "latency_ms": latency_ms,
        "status_code": status_code,
        "is_valid_json": is_valid_json,
        "has_stack_trace": has_stack_trace,
        "error_msg": error_msg,
    }


# ── Scenario runner ──────────────────────────────────────────────────────────

async def run_scenario(
    name: str,
    concurrency: int,
    duration_s: float,
    endpoint: str,
    api_key: Optional[str],
    second_endpoint: Optional[str] = None,
    second_concurrency: Optional[int] = None,
) -> dict:
    """Run one load scenario using a semaphore-bounded approach.

    Each 'worker slot' loops over: acquire semaphore, fire request, release,
    repeat until deadline. No queue/feeder -- avoids deadlock on slow endpoints.
    """
    second_concurrency = second_concurrency or 0
    total_concurrency = concurrency + second_concurrency

    limits = httpx.Limits(
        max_connections=max(total_concurrency * 2, 400),
        max_keepalive_connections=max(total_concurrency * 2, 400),
    )

    latencies: list[float] = []
    status_counts: dict[int, int] = {}
    errors: list[str] = []
    first_error: Optional[str] = None
    has_stack_trace = False
    lock = asyncio.Lock()

    async def record(result: dict):
        nonlocal first_error, has_stack_trace
        async with lock:
            latencies.append(result["latency_ms"])
            sc = result["status_code"]
            status_counts[sc] = status_counts.get(sc, 0) + 1
            if result["has_stack_trace"]:
                has_stack_trace = True
            if result["error_msg"]:
                errors.append(result["error_msg"])
                if first_error is None:
                    first_error = result["error_msg"]

    async def worker_loop(slot_idx: int, ep: str):
        req_idx = slot_idx
        deadline = time.perf_counter() + duration_s
        while time.perf_counter() < deadline:
            result = await _single_request(client, ep, api_key, req_idx)
            await record(result)
            req_idx += total_concurrency

    async with httpx.AsyncClient(limits=limits, timeout=120.0) as client:
        tasks = []
        for i in range(concurrency):
            tasks.append(asyncio.create_task(worker_loop(i, endpoint)))
        if second_endpoint:
            for i in range(second_concurrency):
                tasks.append(asyncio.create_task(worker_loop(
                    concurrency + i, second_endpoint
                )))
        await asyncio.gather(*tasks)

    # Compute stats
    total_requests = len(latencies)
    req_per_sec = total_requests / duration_s if duration_s > 0 else 0

    sorted_lat = sorted(latencies)
    p50 = _percentile(sorted_lat, 50)
    p95 = _percentile(sorted_lat, 95)
    p99 = _percentile(sorted_lat, 99)
    avg = sum(latencies) / len(latencies) if latencies else 0
    max_lat = max(latencies) if latencies else 0

    error_count = sum(v for k, v in status_counts.items() if k != 200)
    unique_errors = list(dict.fromkeys(errors))[:3]

    return {
        "scenario": name,
        "endpoint": endpoint,
        "second_endpoint": second_endpoint,
        "concurrency": total_concurrency,
        "duration_s": duration_s,
        "total_requests": total_requests,
        "req_per_sec": round(req_per_sec, 1),
        "latency_ms": {
            "avg": round(avg, 1),
            "p50": round(p50, 1),
            "p95": round(p95, 1),
            "p99": round(p99, 1),
            "max": round(max_lat, 1),
        },
        "status_counts": status_counts,
        "error_count": error_count,
        "has_stack_trace": has_stack_trace,
        "first_error": first_error,
        "unique_errors": unique_errors,
        "tail_ratio_p99_p50": round(p99 / p50, 1) if p50 > 0 else None,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

async def run_all_scenarios(args) -> dict:
    api_key = _read_api_key()
    pid = _get_worker_pid()

    # Pre-stress measurements
    pre_rss = _get_rss_kb(pid) if pid else None
    pre_fds = _get_fd_count(pid) if pid else None
    pre_log_lines = _log_line_count()

    print(f"\nPre-stress  |  PID={pid}  RSS={pre_rss}KB  FDs={pre_fds}  log_lines={pre_log_lines}")
    print(f"API key:    {'found (' + api_key[:16] + '...)' if api_key else 'NOT FOUND'}")
    print()

    scenario_filter = args.scenario if hasattr(args, 'scenario') and args.scenario != "all" else None

    all_results = []

    # Scenario 1: Light load
    if scenario_filter in (None, "light"):
        print("[1/4] Light load: 10 concurrent, 10s, /api/memory/search")
        sys.stdout.flush()
        r = await run_scenario(
            name="light",
            concurrency=10,
            duration_s=10,
            endpoint="/api/memory/search",
            api_key=api_key,
        )
        all_results.append(r)
        _print_scenario(r)

    # Scenario 2: Heavy load
    if scenario_filter in (None, "heavy"):
        print("[2/4] Heavy load: 100 concurrent, 30s, /api/memory/search")
        sys.stdout.flush()
        r = await run_scenario(
            name="heavy",
            concurrency=100,
            duration_s=30,
            endpoint="/api/memory/search",
            api_key=api_key,
        )
        all_results.append(r)
        _print_scenario(r)

    # Scenario 3: Auth failure under load
    if scenario_filter in (None, "auth"):
        print("[3/4] Auth fail: 100 concurrent, 10s, missing key (expect 100% 401)")
        sys.stdout.flush()
        r = await run_scenario(
            name="auth_fail",
            concurrency=100,
            duration_s=10,
            endpoint="/api/memory/search",
            api_key=None,  # intentionally no key
        )
        all_results.append(r)
        _print_scenario(r, expect_401=True)

    # Scenario 4: Mixed cross-endpoint contention
    if scenario_filter in (None, "mixed"):
        print("[4/4] Mixed: 50 /api/memory/search + 50 /api/observations/recent")
        sys.stdout.flush()
        r = await run_scenario(
            name="mixed",
            concurrency=50,
            duration_s=20,
            endpoint="/api/memory/search",
            api_key=api_key,
            second_endpoint="/api/observations/recent",
            second_concurrency=50,
        )
        all_results.append(r)
        _print_scenario(r)

    # Custom scenario (from CLI args)
    if scenario_filter not in (None, "light", "heavy", "auth", "mixed"):
        endpoint_map = {
            "search": "/api/memory/search",
            "recent": "/api/observations/recent",
            "stats": "/api/stats",
        }
        ep = endpoint_map.get(args.endpoint, f"/api/{args.endpoint}")
        print(f"[custom] {args.concurrency} concurrent, {args.duration}s, {ep}")
        sys.stdout.flush()
        r = await run_scenario(
            name="custom",
            concurrency=args.concurrency,
            duration_s=args.duration,
            endpoint=ep,
            api_key=api_key,
        )
        all_results.append(r)
        _print_scenario(r)

    # Post-stress measurements
    post_rss = _get_rss_kb(pid) if pid else None
    post_fds = _get_fd_count(pid) if pid else None
    post_log_lines = _log_line_count()

    rss_delta = (post_rss - pre_rss) if (pre_rss and post_rss) else None
    fd_delta = (post_fds - pre_fds) if (pre_fds and post_fds) else None
    log_delta = (post_log_lines - pre_log_lines) if (pre_log_lines and post_log_lines) else None

    print(f"\nPost-stress |  PID={pid}  RSS={post_rss}KB  FDs={post_fds}  log_lines={post_log_lines}")
    print(f"Deltas      |  RSS_delta={rss_delta}KB  FD_delta={fd_delta}  log_lines_delta={log_delta}")

    # Health check
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{WORKER_URL}/api/health",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
            health_ok = resp.status_code == 200
            health_body = resp.json()
    except Exception as e:
        health_ok = False
        health_body = {"error": str(e)}

    print(f"\nPost-stress health: {'OK' if health_ok else 'FAIL'}  {health_body}")

    # Bug analysis
    bugs = _analyze_bugs(all_results, rss_delta, fd_delta)
    if bugs:
        print("\n=== BUGS FOUND ===")
        for b in bugs:
            print(f"  BUG-DD-{b['id']:02d}: {b['severity']} -- {b['description']}")
    else:
        print("\n=== No bugs found ===")

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pass": "DD",
        "worker_pid": pid,
        "pre_stress": {"rss_kb": pre_rss, "fd_count": pre_fds, "log_lines": pre_log_lines},
        "post_stress": {"rss_kb": post_rss, "fd_count": post_fds, "log_lines": post_log_lines},
        "deltas": {"rss_kb": rss_delta, "fd_count": fd_delta, "log_lines": log_delta},
        "health_ok": health_ok,
        "health_body": health_body,
        "scenarios": all_results,
        "bugs": bugs,
    }


def _print_scenario(r: dict, expect_401: bool = False):
    lat = r["latency_ms"]
    tail_ratio = r.get("tail_ratio_p99_p50")
    tail_warn = (
        f"  *** TAIL p99/p50={tail_ratio}x >10x (pathological)"
        if tail_ratio and tail_ratio > 10 else ""
    )

    print(f"  Requests:  {r['total_requests']} total, {r['req_per_sec']}/s")
    print(
        f"  Latency:   avg={lat['avg']}ms  p50={lat['p50']}ms  "
        f"p95={lat['p95']}ms  p99={lat['p99']}ms  max={lat['max']}ms{tail_warn}"
    )
    print(f"  Status:    {r['status_counts']}")

    if expect_401:
        total_req = r["total_requests"]
        got_401 = r["status_counts"].get(401, 0)
        pct = (got_401 / total_req * 100) if total_req > 0 else 0
        result_label = "PASS" if pct > 99 else "FAIL"
        print(f"  Auth:      {got_401}/{total_req} ({pct:.1f}%) were 401  {result_label}")

    if r["has_stack_trace"]:
        print(f"  STACK TRACE in error body: {r['first_error']}")
    elif r["first_error"]:
        print(f"  First error: {r['first_error'][:120]}")
    print()


def _analyze_bugs(
    results: list[dict],
    rss_delta: Optional[int],
    fd_delta: Optional[int],
) -> list[dict]:
    bugs = []
    bug_id = 1

    for r in results:
        # 1. Stack traces in error bodies
        if r["has_stack_trace"]:
            bugs.append({
                "id": bug_id,
                "severity": "HIGH",
                "scenario": r["scenario"],
                "description": (
                    f"Stack trace leaked in error response body on {r['endpoint']}: "
                    f"{r['first_error'][:200]}"
                ),
            })
            bug_id += 1

        # 2. Tail latency ratio (p99/p50 > 10x)
        tail = r.get("tail_ratio_p99_p50")
        if tail and tail > 10:
            bugs.append({
                "id": bug_id,
                "severity": "MEDIUM",
                "scenario": r["scenario"],
                "description": (
                    f"Tail latency pathology on {r['endpoint']}: "
                    f"p99/p50={tail}x (p50={r['latency_ms']['p50']}ms, "
                    f"p99={r['latency_ms']['p99']}ms). "
                    f"Likely db_lock serializing reads unnecessarily."
                ),
            })
            bug_id += 1

        # 3. Auth failure under load: non-401 responses that are NOT connection errors
        # Connection errors (status < 0) under extreme load are a capacity finding,
        # not an auth bypass. Only flag actual 2xx/3xx/5xx non-401s.
        if r["scenario"] == "auth_fail":
            total = r["total_requests"]
            got_401 = r["status_counts"].get(401, 0)
            conn_err = sum(v for k, v in r["status_counts"].items() if k < 0)
            auth_bypass = total - got_401 - conn_err  # only actual bypasses
            if auth_bypass > 0:
                bypass_statuses = {k: v for k, v in r["status_counts"].items()
                                   if k >= 0 and k != 401}
                bugs.append({
                    "id": bug_id,
                    "severity": "HIGH",
                    "scenario": r["scenario"],
                    "description": (
                        f"Auth bypass under load: {auth_bypass}/{total} requests "
                        f"returned non-401 non-connection-error status. "
                        f"Suspicious statuses: {bypass_statuses}"
                    ),
                })
                bug_id += 1
            # Connection errors > 20% under auth stress are a capacity signal, not a bug.
            # Log as informational in the scenario output only.

        # 4. Connection errors (status -1, -2, -3) above 5% -- only for non-auth scenarios
        # at moderate concurrency. At 100 concurrent, overflow is expected.
        # Flag only for the light scenario where concurrency is reasonable.
        conn_errors = sum(v for k, v in r["status_counts"].items() if k < 0)
        total = r["total_requests"]
        if (
            conn_errors > 0
            and r["scenario"] not in ("auth_fail", "heavy", "mixed")
        ):
            pct = conn_errors / total * 100 if total > 0 else 0
            if pct > 5:
                bugs.append({
                    "id": bug_id,
                    "severity": "HIGH",
                    "scenario": r["scenario"],
                    "description": (
                        f"Connection failures at low concurrency ({r['concurrency']}) "
                        f"on {r['endpoint']}: {conn_errors}/{total} ({pct:.1f}%). "
                        f"First error: {r['first_error']}"
                    ),
                })
                bug_id += 1
        elif (
            conn_errors > 0
            and r["scenario"] in ("heavy", "mixed")
        ):
            # Document capacity ceiling as MEDIUM finding (not HIGH -- expected at 100 concurrent)
            pct = conn_errors / total * 100 if total > 0 else 0
            if pct > 50:
                bugs.append({
                    "id": bug_id,
                    "severity": "MEDIUM",
                    "scenario": r["scenario"],
                    "description": (
                        f"Capacity ceiling: uvicorn single-worker rejects "
                        f"{conn_errors}/{total} ({pct:.1f}%) connections at "
                        f"{r['concurrency']}-concurrent load. "
                        f"Consider --limit-concurrency or multiple workers for "
                        f"production multi-user use. Single-user workload is fine."
                    ),
                })
                bug_id += 1

    # 5. Memory growth (RSS grew >200MB) -- SQLite page cache inflation is normal.
    # 200MB threshold distinguishes SQLite mmap loading (~100-200MB typical) from
    # a true application-level leak. Report at >200MB; flag as MEDIUM (not HIGH).
    if rss_delta is not None and rss_delta > 204_800:  # 200MB in KB
        bugs.append({
            "id": bug_id,
            "severity": "MEDIUM",
            "scenario": "all",
            "description": (
                f"RSS grew by {rss_delta}KB ({rss_delta / 1024:.1f}MB) during stress. "
                f"Exceeds 200MB threshold -- could indicate accumulated SQLite page "
                f"cache from {_get_worker_pid() or 'worker'} MemoryRetriever instances "
                f"not being garbage collected. Verify with extended idle test."
            ),
        })
        bug_id += 1

    # 6. FD leak (>50 new file descriptors)
    if fd_delta is not None and fd_delta > 50:
        bugs.append({
            "id": bug_id,
            "severity": "MEDIUM",
            "scenario": "all",
            "description": (
                f"FD count grew by {fd_delta} during stress. "
                f"Possible connection leak in _get_retriever() -- SQLite handles not closed."
            ),
        })
        bug_id += 1

    return bugs


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pass DD stress test for cortex memory worker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Scenarios (run all by default):
  light   10 concurrent, 10s, /api/memory/search
  heavy   100 concurrent, 30s, /api/memory/search
  auth    100 concurrent, no key, /api/memory/search (expect 100% 401)
  mixed   50 /api/memory/search + 50 /api/observations/recent

Custom mode (provide --concurrency, --duration, --endpoint):
  --endpoint search|recent|stats
        """,
    )
    parser.add_argument(
        "--scenario",
        choices=["light", "heavy", "auth", "mixed", "all"],
        default="all",
        help="Which scenario to run (default: all)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=50,
        help="Custom mode: concurrent workers (default: 50, max: 200)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30,
        help="Custom mode: duration in seconds (default: 30)",
    )
    parser.add_argument(
        "--endpoint",
        choices=["search", "recent", "stats"],
        default="search",
        help="Custom mode: endpoint to hit (default: search)",
    )

    args = parser.parse_args()

    if args.concurrency > 200:
        print("ERROR: --concurrency > 200 is blocked (hard limit).", file=sys.stderr)
        sys.exit(1)

    print(f"Pass DD Stress Test -- {datetime.now(timezone.utc).isoformat()}")
    print("=" * 72)
    print(f"Target: {WORKER_URL}")

    # Verify worker is up before starting
    import urllib.request
    try:
        resp = urllib.request.urlopen(f"{WORKER_URL}/api/health", timeout=5)
        health = json.loads(resp.read())
        print(f"Worker health: OK  {health}")
    except Exception as e:
        print(f"ERROR: Worker not reachable at {WORKER_URL}: {e}", file=sys.stderr)
        sys.exit(1)

    results = asyncio.run(run_all_scenarios(args))

    # Save results
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = EVAL_DIR / f"stress_test_{ts}.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nResults saved to: {out_path}")

    # Exit code: 0 if no bugs, 1 if bugs found
    return 0 if not results.get("bugs") else 1


if __name__ == "__main__":
    sys.exit(main())
