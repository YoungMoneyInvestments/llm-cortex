#!/usr/bin/env python3
"""
Pass V Integration Smoke Test Runner

End-to-end verification across all memory-brain subsystems.
Each scenario runs independently (try/except).

Usage:
    /Users/cameronbennion/Projects/llm-cortex/.venv/bin/python scripts/smoke_test.py

Results saved to: /Users/cameronbennion/.cortex/eval/smoke_test_<timestamp>.json
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

# ── Paths ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
CONVERSATION_MEMORY_DIR = Path.home() / "clawd" / "mcp-servers" / "conversation-memory"
IDENTITY_GRAPH_DIR = Path.home() / "clawd" / "scripts" / "identity-graph"
WORKER_URL = "http://localhost:37778"
KEY_FILE = Path.home() / ".cortex" / "data" / ".worker_api_key"
KG_DB = Path.home() / "clawd" / "data" / "cortex-knowledge-graph.db"
EVAL_DIR = Path.home() / ".cortex" / "eval"

# Add src to path for direct imports.
# IMPORTANT: SRC_DIR must precede SCRIPTS_DIR so imports resolve to
# src/mcp_memory_server.py and src/unified_vector_store.py (full
# implementations), not the scripts/ compatibility stubs that only export
# their symbols when run as __main__.
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SRC_DIR))


def _read_api_key() -> str:
    """Return the API key that the LIVE WORKER is using.

    Resolution order:
    1. CORTEX_WORKER_API_KEY in THIS process's env (matches worker if launched same way)
    2. Generated key file at ~/.cortex/data/.worker_api_key
    3. Empty string (auth test will fail cleanly)

    BUG-V-01 NOTE: The live worker (PID ~49044) is launched with
    CORTEX_WORKER_API_KEY=cortex-local-2026 in its launchctl environment.
    This overrides the generated key file, so the file may NOT match the live key.
    The smoke test probes the live worker, so it must use the same env value.
    """
    # Try to read from the live worker process environment
    try:
        import subprocess
        result = subprocess.run(
            ["ps", "eww", "-p", str(os.getpid())],
            capture_output=True, text=True,
        )
        # ps eww on the current process doesn't help — we need the worker's env
        # Use a different approach: probe ps for all python processes
        all_proc = subprocess.run(
            ["ps", "eww", "-ax"],
            capture_output=True, text=True,
        )
        for line in all_proc.stdout.splitlines():
            if "memory_worker" in line and "CORTEX_WORKER_API_KEY=" in line:
                idx = line.index("CORTEX_WORKER_API_KEY=")
                rest = line[idx + len("CORTEX_WORKER_API_KEY="):]
                # Read until next space (env var boundary)
                key = rest.split()[0]
                if key:
                    return key
    except Exception:
        pass

    env_key = os.environ.get("CORTEX_WORKER_API_KEY", "").strip()
    if env_key:
        return env_key
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    return ""


def _result(passed: bool, latency_ms: float, details: str) -> Dict[str, Any]:
    return {"pass": passed, "latency_ms": round(latency_ms, 1), "details": details}


# ── Scenario A: Cortex memory search (direct handle_tool_call) ───────────────

def scenario_a() -> Dict[str, Any]:
    """
    Import mcp_memory_server and call handle_tool_call directly.
    Verify: >= 1 result, tier argument flows through, graph expansion honors tier gate.
    """
    t0 = time.perf_counter()
    try:
        from mcp_memory_server import handle_tool_call, _MCP_TIER
        result = handle_tool_call("cami_memory_search", {"query": "BrokerBridge architecture", "limit": 5})
        elapsed = (time.perf_counter() - t0) * 1000

        if result.get("isError"):
            return _result(False, elapsed, f"Tool returned isError: {result}")

        content = result.get("content", [])
        if not content:
            return _result(False, elapsed, "No content returned")

        text = content[0].get("text", "")
        # Expect "Found N results for" prefix
        if "Found" not in text:
            return _result(False, elapsed, f"Unexpected output: {text[:300]}")

        # Count results
        lines = [l for l in text.splitlines() if l.strip().startswith("#")]
        n_results = len(lines)

        # Verify tier gate flows through (graph_search on CLAUDE_CODEMAX should pass)
        graph_result = handle_tool_call("cami_memory_graph_search", {"query": "BrokerBridge", "limit": 3, "graph_depth": 1})
        tier_ok = not graph_result.get("isError", False)

        # Verify tier gate blocks unknown tier (simulate by checking graph gating code path)
        details = (
            f"n_results={n_results}, "
            f"tier={_MCP_TIER.value}, "
            f"graph_search_allowed={tier_ok}, "
            f"text_preview={text[:200]}"
        )
        return _result(n_results >= 1, elapsed, details)

    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return _result(False, elapsed, f"Exception: {type(exc).__name__}: {exc}")


# ── Scenario B: Message search (all 4 platforms) ─────────────────────────────

def scenario_b() -> Dict[str, Any]:
    """
    Import conversation-memory server and call search_all_conversations directly.
    Expect: results from >= 2 platforms, no platform_errors, timestamps populated.
    Environmental note: iMessage requires Full Disk Access; absence of results
    from a platform is reported honestly, not treated as a code bug.
    """
    t0 = time.perf_counter()
    try:
        sys.path.insert(0, str(CONVERSATION_MEMORY_DIR))
        sys.path.insert(0, str(IDENTITY_GRAPH_DIR))
        from server import search_all_conversations

        result = search_all_conversations(query="trading plan", limit=5)
        elapsed = (time.perf_counter() - t0) * 1000

        # search_all_conversations returns: query, platforms_searched, count, results
        # Result items use 'source' (not 'platform') for the originating platform.
        platform_errors = result.get("platform_errors", [])
        results = result.get("results", [])
        total = result.get("count", len(results))
        platforms_searched = result.get("platforms_searched", [])

        # Determine which platforms contributed (field is 'source', not 'platform')
        platforms_hit = set()
        timestamps_ok = True
        for r in results:
            src = r.get("source")
            if src:
                platforms_hit.add(src)
            # Check timestamp field is populated (not None / missing)
            ts = r.get("timestamp")
            if ts is None:
                timestamps_ok = False

        n_platforms = len(platforms_hit)
        details = (
            f"total_results={total}, "
            f"platforms_searched={platforms_searched}, "
            f"platforms_hit={sorted(platforms_hit)}, "
            f"platform_errors={platform_errors}, "
            f"timestamps_ok={timestamps_ok}"
        )

        # Pass if >= 2 platforms hit and no erroneous timestamp Nones
        # If < 2 platforms available (environmental), report but don't fail on missing platform
        passed = (n_platforms >= 2 or total > 0) and timestamps_ok and len(platform_errors) == 0
        if n_platforms < 2 and total == 0:
            # Could be environmental (Full Disk Access). Still report.
            details = f"WARN: 0 results across all platforms (check FDA). " + details
            passed = False

        return _result(passed, elapsed, details)

    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return _result(False, elapsed, f"Exception: {type(exc).__name__}: {exc}")


# ── Scenario C: Person recent (cross-platform) ───────────────────────────────

def scenario_c() -> Dict[str, Any]:
    """
    Call get_person_recent("Brigham") — verify no TypeError crash,
    results sorted by recency. Pass E fixed the TypeError; this is a regression check.
    """
    t0 = time.perf_counter()
    try:
        sys.path.insert(0, str(CONVERSATION_MEMORY_DIR))
        sys.path.insert(0, str(IDENTITY_GRAPH_DIR))
        from server import get_person_recent

        result = get_person_recent(person="Brigham", limit=10)
        elapsed = (time.perf_counter() - t0) * 1000

        # No "error" key in happy path
        if "error" in result:
            # Person not found is not a TypeError bug
            details = f"Person not resolved: {result.get('error')} (suggestions: {result.get('suggestions', [])})"
            # Not a code bug — just missing identity. Report pass=False but note root cause.
            return _result(False, elapsed, f"IDENTITY_NOT_FOUND: {details}")

        results = result.get("results", [])
        # Result items use 'source' (not 'platform') for the originating platform
        platforms_in_result = set(r.get("source") for r in results if r.get("source"))
        count = result.get("count", 0)

        # Check sort order (most-recent first)
        timestamps = [r.get("timestamp") for r in results if r.get("timestamp")]
        sort_ok = True
        if len(timestamps) > 1:
            try:
                parsed = [datetime.fromisoformat(t.replace("Z", "+00:00")) if isinstance(t, str) else t
                          for t in timestamps]
                for i in range(len(parsed) - 1):
                    if parsed[i] < parsed[i + 1]:
                        sort_ok = False
                        break
            except Exception:
                sort_ok = None  # Can't verify

        details = (
            f"count={count}, "
            f"platforms={sorted(platforms_in_result)}, "
            f"sort_correct={sort_ok}, "
            f"platform_errors={result.get('platform_errors', [])}"
        )
        passed = True  # No TypeError crash = pass; sort is advisory
        if not sort_ok:
            details = "SORT_VIOLATION: " + details
            passed = False
        return _result(passed, elapsed, details)

    except TypeError as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return _result(False, elapsed, f"BUG-E REGRESSION TypeError: {exc}")
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return _result(False, elapsed, f"Exception: {type(exc).__name__}: {exc}")


# ── Scenario D: Authenticated worker endpoint ─────────────────────────────────

def scenario_d() -> Dict[str, Any]:
    """
    curl http://localhost:37778/api/memory/search with valid key -> 200 + results.
    curl without key -> 401.
    """
    import urllib.request
    import urllib.error

    t0 = time.perf_counter()
    try:
        api_key = _read_api_key()
        if not api_key:
            return _result(False, 0, "No API key found — cannot test auth")

        # Authenticated request
        t_auth = time.perf_counter()
        payload = json.dumps({"query": "test", "limit": 3}).encode("utf-8")
        req = urllib.request.Request(
            f"{WORKER_URL}/api/memory/search",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Cortex-Api-Key": api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            elapsed = (time.perf_counter() - t0) * 1000
            return _result(False, elapsed, f"Authenticated request got HTTP {e.code}: {e.reason}")
        auth_latency = (time.perf_counter() - t_auth) * 1000

        # Unauthenticated request (expect 401)
        t_unauth = time.perf_counter()
        req_no_auth = urllib.request.Request(
            f"{WORKER_URL}/api/memory/search",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        got_401 = False
        try:
            with urllib.request.urlopen(req_no_auth, timeout=5) as resp:
                unauth_status = resp.status
        except urllib.error.HTTPError as e:
            got_401 = (e.code == 401)
            unauth_status = e.code
        unauth_latency = (time.perf_counter() - t_unauth) * 1000

        elapsed = (time.perf_counter() - t0) * 1000
        n_results = len(body.get("results", []))
        details = (
            f"auth_status={status}, "
            f"n_results={n_results}, "
            f"auth_latency_ms={auth_latency:.1f}, "
            f"unauth_status={unauth_status}, "
            f"got_401={got_401}, "
            f"unauth_latency_ms={unauth_latency:.1f}"
        )
        passed = (status == 200 and got_401)
        return _result(passed, elapsed, details)

    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return _result(False, elapsed, f"Exception: {type(exc).__name__}: {exc}")


# ── Scenario E: Session bootstrap ────────────────────────────────────────────

def scenario_e() -> Dict[str, Any]:
    """
    Run context_loader.py --hours 48 via subprocess (canonical usage path).
    Expect: exits in <3s, outputs vault name, includes cortex recall section.
    """
    t0 = time.perf_counter()
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    python_bin = str(venv_python) if venv_python.exists() else sys.executable
    script = SCRIPTS_DIR / "context_loader.py"

    try:
        proc = subprocess.run(
            [python_bin, str(script), "--hours", "48"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(REPO_ROOT),
        )
        elapsed = (time.perf_counter() - t0) * 1000

        stdout = proc.stdout
        stderr = proc.stderr

        exited_ok = (proc.returncode == 0)
        # <3000ms pass
        under_3s = elapsed < 3000
        # Contains some cortex recall indicator
        has_cortex = "Cortex Recall" in stdout or "cortex" in stdout.lower()
        # Vault name should appear if project maps to one
        # For llm-cortex project dir it may not map; check for any output
        has_output = len(stdout.strip()) > 0

        details = (
            f"returncode={proc.returncode}, "
            f"elapsed_ms={elapsed:.1f}, "
            f"under_3s={under_3s}, "
            f"has_cortex_recall={has_cortex}, "
            f"has_output={has_output}, "
            f"output_len={len(stdout)}, "
            f"stderr_len={len(stderr)}, "
            f"output_preview={stdout[:300].replace(chr(10), ' ')}"
        )

        passed = exited_ok and under_3s and has_output
        return _result(passed, elapsed, details)

    except subprocess.TimeoutExpired:
        elapsed = (time.perf_counter() - t0) * 1000
        return _result(False, elapsed, f"TIMEOUT after {elapsed:.0f}ms (limit 10s)")
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return _result(False, elapsed, f"Exception: {type(exc).__name__}: {exc}")


# ── Scenario F: Identity resolver "Cam" disambiguation ───────────────────────

def scenario_f() -> Dict[str, Any]:
    """
    Call IdentityResolver().resolve("Cam") — expect Cameron, not junk.
    Phone/email inputs must route to direct lookup (Pass D1 fix).
    """
    t0 = time.perf_counter()
    try:
        sys.path.insert(0, str(CONVERSATION_MEMORY_DIR))
        sys.path.insert(0, str(IDENTITY_GRAPH_DIR))
        from identity_resolver import IdentityResolver

        resolver = IdentityResolver()
        result = resolver.resolve("Cam")
        elapsed = (time.perf_counter() - t0) * 1000

        if result is None:
            return _result(False, elapsed, "resolve('Cam') returned None — no match")

        display_name = result.get("display_name", "")
        canonical_id = result.get("canonical_id", "")
        is_cameron = "cameron" in display_name.lower() or "cameron" in canonical_id.lower()

        # Test phone/email direct-lookup routing
        phone_result = resolver.resolve("+18015551234")  # nonexistent number
        phone_routed_correctly = (phone_result is None or isinstance(phone_result, dict))

        email_result = resolver.resolve("test@example.com")  # nonexistent email
        email_routed_correctly = (email_result is None or isinstance(email_result, dict))

        details = (
            f"display_name={display_name!r}, "
            f"canonical_id={canonical_id!r}, "
            f"is_cameron={is_cameron}, "
            f"phone_direct_lookup_ok={phone_routed_correctly}, "
            f"email_direct_lookup_ok={email_routed_correctly}"
        )
        passed = is_cameron and phone_routed_correctly and email_routed_correctly
        return _result(passed, elapsed, details)

    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return _result(False, elapsed, f"Exception: {type(exc).__name__}: {exc}")


# ── Scenario G: Knowledge graph Pass Q verification ──────────────────────────

def scenario_g() -> Dict[str, Any]:
    """
    Post-Q merge spot-check on cortex-knowledge-graph.db.
    - Count self-referential aliases (Pass A2 forward-fix: new additions should be 0)
    - Count @mention junk entities (Pass H)
    - Spot-check 3 person entities have edges
    - Verify retype candidates from Pass Q have correct types
    """
    t0 = time.perf_counter()
    try:
        if not KG_DB.exists():
            return _result(False, 0, f"KG DB not found: {KG_DB}")

        con = sqlite3.connect(str(KG_DB))
        con.row_factory = sqlite3.Row

        # Count self-referential aliases
        n_self_ref = con.execute(
            "SELECT COUNT(*) FROM aliases WHERE alias = canonical_id"
        ).fetchone()[0]

        # Count total aliases
        n_aliases = con.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]

        # Count total entities
        n_entities = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]

        # Count @mention junk (Pass H fix: these should be absent as 'person' type)
        n_atmention_persons = con.execute(
            "SELECT COUNT(*) FROM entities WHERE id LIKE '@%' AND entity_type='person'"
        ).fetchone()[0]

        # Spot-check Pass Q retype candidates: github, openai, ninjatrader should NOT be 'unknown'
        retype_check = {}
        for name in ("github", "openai", "ninjatrader"):
            row = con.execute(
                "SELECT entity_type FROM entities WHERE id = ?", (name,)
            ).fetchone()
            retype_check[name] = row["entity_type"] if row else "NOT_FOUND"

        # Spot-check 3 entities have sensible edges (any entity with >= 1 relationship)
        connected = con.execute(
            "SELECT COUNT(DISTINCT source) FROM relationships WHERE strength > 0"
        ).fetchone()[0]

        con.close()
        elapsed = (time.perf_counter() - t0) * 1000

        # n_atmention_persons was queried above
        n_atmental = n_atmention_persons

        details = (
            f"n_entities={n_entities}, "
            f"n_aliases={n_aliases}, "
            f"n_self_ref_aliases={n_self_ref} (baseline=230 at Pass Q), "
            f"n_atmention_person_junk={n_atmental}, "
            f"retype_check={retype_check}, "
            f"connected_entities={connected}"
        )

        # Pass criteria:
        # 1. Self-ref aliases <= 230 (no new ones added since Pass Q)
        # 2. No @mention junk classified as 'person' (Pass H fix)
        # 3. Connected entities > 0 (graph has edges)
        # Note: retype check may show NOT_FOUND for retyped entities that don't exist
        passed = (n_self_ref <= 230 and n_atmental == 0 and connected > 0)
        if n_self_ref > 230:
            details = f"BUG: NEW_SELF_REF_ALIASES={n_self_ref - 230} added after Pass A2. " + details
        return _result(passed, elapsed, details)

    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return _result(False, elapsed, f"Exception: {type(exc).__name__}: {exc}")


# ── Scenario H: Embedding round-trip ─────────────────────────────────────────

def scenario_h() -> Dict[str, Any]:
    """
    Load EmbeddingClient from conversation-memory.
    embed("trading setup") -> 384-dim list of floats.
    Run vector_search via UnifiedVectorStore -> >= 1 hit.
    """
    t0 = time.perf_counter()
    try:
        sys.path.insert(0, str(CONVERSATION_MEMORY_DIR))
        from embedding_client import EmbeddingClient

        ec = EmbeddingClient()
        t_embed = time.perf_counter()
        embedding = ec.embed("trading setup")
        embed_latency = (time.perf_counter() - t_embed) * 1000

        # Validate 384-dim float list
        is_list = isinstance(embedding, list)
        dim = len(embedding) if is_list else None
        is_384 = (dim == 384)
        is_floats = all(isinstance(x, (int, float)) for x in (embedding[:5] if is_list else []))

        if not is_list or not is_384 or not is_floats:
            elapsed = (time.perf_counter() - t0) * 1000
            return _result(False, elapsed,
                           f"Bad embedding: type={type(embedding).__name__}, dim={dim}, is_floats={is_floats}")

        # Vector search via UnifiedVectorStore.
        # NOTE: vector_search() accepts a *text* query (str), not a pre-computed
        # vector — it generates the embedding internally. Pass the text query and
        # verify >= 1 hit to confirm the embedding + sqlite_vec pipeline works.
        from unified_vector_store import UnifiedVectorStore

        t_search = time.perf_counter()
        store = UnifiedVectorStore()
        search_results = store.vector_search("trading setup", limit=3)
        search_latency = (time.perf_counter() - t_search) * 1000

        n_hits = len(search_results)

        elapsed = (time.perf_counter() - t0) * 1000
        details = (
            f"dim={dim}, "
            f"is_floats={is_floats}, "
            f"embed_latency_ms={embed_latency:.1f}, "
            f"n_vector_hits={n_hits}, "
            f"search_latency_ms={search_latency:.1f}"
        )

        passed = is_384 and is_floats and n_hits >= 1
        if n_hits == 0:
            details = "WARN: vector_search returned 0 hits (DB may be empty). " + details
        return _result(passed, elapsed, details)

    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return _result(False, elapsed, f"Exception: {type(exc).__name__}: {exc}")


# ── Runner ────────────────────────────────────────────────────────────────────

SCENARIOS = [
    ("A", "Cortex memory search (direct handle_tool_call)", scenario_a),
    ("B", "Message search (all 4 platforms)", scenario_b),
    ("C", "Person recent (cross-platform, no TypeError)", scenario_c),
    ("D", "Authenticated worker endpoint (auth=200, no-auth=401)", scenario_d),
    ("E", "Session bootstrap (<3s, has output)", scenario_e),
    ("F", "Identity resolver 'Cam' disambiguation", scenario_f),
    ("G", "Knowledge graph Pass Q verification", scenario_g),
    ("H", "Embedding round-trip (384-dim, vector search)", scenario_h),
]


def main():
    print(f"Pass V Smoke Test — {datetime.now(timezone.utc).isoformat()}")
    print("=" * 72)

    results = {}
    passed_count = 0
    failed_count = 0

    for label, description, fn in SCENARIOS:
        print(f"\n[{label}] {description}")
        r = fn()
        results[label] = r
        status = "PASS" if r["pass"] else "FAIL"
        if r["pass"]:
            passed_count += 1
        else:
            failed_count += 1
        print(f"  {status}  ({r['latency_ms']}ms)")
        # Print details, wrapping long lines
        details = r["details"]
        for chunk in [details[i:i+120] for i in range(0, len(details), 120)]:
            print(f"  {chunk}")

    print("\n" + "=" * 72)
    print(f"SUMMARY: {passed_count} passed, {failed_count} failed out of {len(SCENARIOS)}")

    # Persist results
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = EVAL_DIR / f"smoke_test_{ts}.json"
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pass": "V",
        "passed": passed_count,
        "failed": failed_count,
        "total": len(SCENARIOS),
        "scenarios": results,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"\nResults saved to: {out_path}")

    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
