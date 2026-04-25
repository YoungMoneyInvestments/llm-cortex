#!/usr/bin/env python3
"""Promote curated Cortex artifacts into the matching Obsidian project vault.

Exports:
- project-matched session summaries -> <vault>/sessions/
- checked-in markdown reports       -> <vault>/research/

This keeps Cortex as the raw system of record while giving Obsidian a curated,
human-readable layer that the existing bootstrap can already read back in.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

sys.path.insert(0, str(Path(__file__).parent))
from obsidian_bridge import (
    build_project_markers,
    read_text_with_timeout,
    resolve_project_root,
    resolve_vault_folder,
)

MANAGED_START = "<!-- promote_to_obsidian:start -->"
MANAGED_END = "<!-- promote_to_obsidian:end -->"
MANUAL_SECTION = "\n\n## Manual Notes\n\n"


def default_db_path() -> Path:
    return Path.home() / ".cortex" / "data" / "cortex-observations.db"


def parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def safe_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    items = []
    for item in parsed:
        text = str(item).strip()
        if text:
            items.append(text)
    return items


def session_text_blob(row: sqlite3.Row) -> str:
    parts = [
        row["summary"] or "",
        row["user_prompt"] or "",
        " ".join(safe_json_list(row["key_decisions"])),
        " ".join(safe_json_list(row["entities_mentioned"])),
    ]
    return "\n".join(parts).lower()


def marker_hits(text: str, marker: str) -> int:
    pattern = re.compile(rf"(?<![a-z0-9_-]){re.escape(marker)}(?![a-z0-9_-])")
    return len(pattern.findall(text))


def project_focus_score(row: sqlite3.Row, markers: Sequence[str]) -> int:
    summary = (row["summary"] or "").lower()
    prompt = (row["user_prompt"] or "").lower()
    key_decisions = [item.lower() for item in safe_json_list(row["key_decisions"])]
    entities = [item.lower() for item in safe_json_list(row["entities_mentioned"])]

    summary_hits = sum(marker_hits(summary, marker) for marker in markers)
    prompt_hits = sum(marker_hits(prompt, marker) for marker in markers)
    key_decision_hits = sum(
        1
        for item in key_decisions
        if any(marker_hits(item, marker) for marker in markers)
    )
    entity_hits = sum(
        1
        for item in entities
        if any(marker_hits(item, marker) for marker in markers)
    )

    score = 0
    score += min(summary_hits, 5)
    score += min(prompt_hits * 5, 10)
    score += min(key_decision_hits * 3, 9)
    score += min(entity_hits * 2, 10)

    # Penalize giant mixed sessions whose extracted decisions never mention the
    # current project even though the summary/body does. Those tend to be noisy
    # aggregate sessions, not project-focused notes.
    if key_decisions and key_decision_hits == 0 and entity_hits > 0:
        score -= min(len(key_decisions), 5) * 2

    return score


def session_matches_project(row: sqlite3.Row, markers: Sequence[str]) -> bool:
    return project_focus_score(row, markers) >= 6


def load_session_candidates(
    db_path: Path,
    hours: int,
) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                ss.id AS summary_id,
                ss.session_id,
                ss.summary,
                ss.key_decisions,
                ss.entities_mentioned,
                ss.created_at,
                s.agent,
                s.user_prompt,
                s.observation_count
            FROM session_summaries ss
            JOIN sessions s ON s.id = ss.session_id
            ORDER BY ss.created_at DESC
            """
        ).fetchall()
    finally:
        conn.close()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    kept = []
    for row in rows:
        created = parse_created_at(row["created_at"])
        if created is None or created < cutoff:
            continue
        kept.append(row)
    kept.sort(
        key=lambda row: (
            parse_created_at(row["created_at"]) or datetime.min.replace(tzinfo=timezone.utc),
            row["summary_id"] or 0,
        ),
        reverse=True,
    )
    return kept


def is_substantive_session(
    row: sqlite3.Row,
    min_observations: int,
    min_summary_chars: int = 120,
) -> bool:
    """Accept sessions that are clearly non-trivial.

    `sessions.observation_count` can lag or remain zero in older rows, so fall
    back to summary length rather than dropping useful historical summaries.
    """

    obs_count = row["observation_count"] or 0
    summary = (row["summary"] or "").strip()
    return obs_count >= min_observations or len(summary) >= min_summary_chars


def render_session_note(row: sqlite3.Row, project_name: str) -> str:
    created = row["created_at"]
    key_decisions = safe_json_list(row["key_decisions"])
    entities = safe_json_list(row["entities_mentioned"])
    lines = [
        "---",
        "source: llm-cortex",
        "note_type: promoted-session-summary",
        f"project: {project_name}",
        f"summary_id: {row['summary_id']}",
        f"session_id: {row['session_id']}",
        f"created_at: {created}",
        f"agent: {row['agent'] or 'unknown'}",
        f"observation_count: {row['observation_count']}",
        "---",
        "",
        f"# Session Summary — {created}",
        "",
        row["summary"].strip(),
    ]
    if row["user_prompt"]:
        lines.extend([
            "",
            "## Initial Prompt",
            "",
            row["user_prompt"].strip(),
        ])
    if key_decisions:
        lines.extend(["", "## Key Decisions", ""])
        lines.extend(f"- {item}" for item in key_decisions)
    if entities:
        lines.extend(["", "## Entities Mentioned", ""])
        lines.extend(f"- {item}" for item in entities)
    lines.append("")
    return "\n".join(lines)


def session_filename(row: sqlite3.Row) -> str:
    created = parse_created_at(row["created_at"]) or datetime.now(timezone.utc)
    summary_id = int(row["summary_id"]) if "summary_id" in row.keys() else 0
    return f"{created:%Y-%m-%d-%H%M%S%f}-{summary_id:08d}-{row['session_id'][:8]}.md"


def render_report_note(project_name: str, report_path: Path, content: str) -> str:
    return "\n".join(
        [
            "---",
            "source: llm-cortex",
            "note_type: promoted-report",
            f"project: {project_name}",
            f"report_file: {report_path.name}",
            f"source_path: {report_path}",
            "---",
            "",
            f"# Research Report — {report_path.name}",
            "",
            content.rstrip(),
            "",
        ]
    )


def managed_note_content(generated_content: str, existing_text: str | None = None) -> str:
    managed_block = f"{MANAGED_START}\n{generated_content.rstrip()}\n{MANAGED_END}"
    if not existing_text:
        return managed_block + MANUAL_SECTION

    if MANAGED_START in existing_text and MANAGED_END in existing_text:
        prefix, _, remainder = existing_text.partition(MANAGED_START)
        _, _, suffix = remainder.partition(MANAGED_END)
        return prefix + managed_block + suffix

    if "source: llm-cortex" in existing_text and "note_type: promoted-" in existing_text:
        legacy_snapshot = existing_text.rstrip()
        if not legacy_snapshot:
            return managed_block + MANUAL_SECTION
        return (
            managed_block
            + MANUAL_SECTION
            + "\n\n## Legacy Note Snapshot\n\n"
            + "_Preserved from a pre-marker promoted note during migration._\n\n"
            + legacy_snapshot
            + "\n"
        )

    raise ValueError("Refusing to overwrite unmanaged Obsidian note")


def read_existing_text(path: Path, timeout_seconds: int = 5) -> str | None:
    return read_text_with_timeout(path, timeout_seconds=timeout_seconds)


def write_text_with_timeout(path: Path, content: str, timeout_seconds: int = 5) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    writer = (
        "from pathlib import Path; "
        "import sys; "
        "Path(sys.argv[1]).write_text(sys.stdin.read(), encoding='utf-8')"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", writer, str(temp_path)],
            input=content,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Timed out writing Obsidian note: {path}") from exc
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Unable to write Obsidian note: {path}") from exc

    if result.returncode != 0:
        temp_path.unlink(missing_ok=True)
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown write error"
        raise RuntimeError(f"Unable to write Obsidian note {path}: {stderr}")
    try:
        finalize = (
            "from pathlib import Path; "
            "import sys; "
            "Path(sys.argv[1]).replace(sys.argv[2])"
        )
        result = subprocess.run(
            [sys.executable, "-c", finalize, str(temp_path), str(path)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Timed out finalizing Obsidian note {path}") from exc
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Unable to finalize Obsidian note {path}") from exc
    if result.returncode != 0:
        temp_path.unlink(missing_ok=True)
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown finalize error"
        raise RuntimeError(f"Unable to finalize Obsidian note {path}: {stderr}")


def write_if_changed(path: Path, generated_content: str, dry_run: bool) -> bool:
    existing_text = read_existing_text(path)
    content = managed_note_content(generated_content, existing_text=existing_text)
    if existing_text == content:
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_with_timeout(path, content)
    return True


def prune_empty_parents(path: Path, stop_dir: Path) -> None:
    current = path
    stop_dir = stop_dir.resolve()
    while current.exists() and current != stop_dir:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def prune_managed_notes(
    base_dir: Path,
    desired_paths: set[Path],
    note_type: str,
    dry_run: bool,
) -> int:
    if not base_dir.exists():
        return 0

    removed = 0
    for existing_path in sorted(base_dir.rglob("*.md")):
        rel_path = existing_path.relative_to(base_dir)
        if rel_path.parts and rel_path.parts[0].startswith("_backup_"):
            continue
        if existing_path in desired_paths:
            continue

        existing_text = read_existing_text(existing_path)
        if not existing_text:
            continue
        is_managed = MANAGED_START in existing_text or (
            "source: llm-cortex" in existing_text and f"note_type: {note_type}" in existing_text
        )
        if not is_managed:
            continue
        if "source: llm-cortex" not in existing_text or f"note_type: {note_type}" not in existing_text:
            continue

        if not dry_run:
            existing_path.unlink()
            prune_empty_parents(existing_path.parent, stop_dir=base_dir)
        removed += 1
    return removed


def tracked_markdown_reports(project_dir: Path, reports_dir: Path) -> list[Path]:
    default_reports_dir = project_dir / "reports"
    if not reports_dir.exists() and reports_dir != default_reports_dir:
        raise RuntimeError(f"Reports directory not found: {reports_dir}")
    try:
        rel_reports = reports_dir.relative_to(project_dir)
    except ValueError as exc:
        raise RuntimeError(f"Reports directory must live inside the project root: {reports_dir}") from exc

    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "ls-tree", "-r", "--name-only", "HEAD", "--", str(rel_reports)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        raise RuntimeError(f"Unable to enumerate committed reports for {project_dir}") from exc

    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise RuntimeError(f"Unable to enumerate committed reports for {project_dir}: {stderr}")

    paths = []
    for rel_path in result.stdout.splitlines():
        if not rel_path.endswith(".md"):
            continue
        paths.append(project_dir / rel_path)
    return sorted(paths)


def read_committed_file(project_dir: Path, file_path: Path) -> str:
    try:
        rel_path = file_path.relative_to(project_dir)
    except ValueError as exc:
        raise ValueError(f"{file_path} is not inside {project_dir}") from exc

    result = subprocess.run(
        ["git", "-C", str(project_dir), "show", f"HEAD:{rel_path.as_posix()}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise RuntimeError(f"Unable to read committed content for {rel_path}: {stderr}")
    return result.stdout


def promote_sessions(
    vault_folder: Path,
    project_dir: Path,
    db_path: Path,
    hours: int,
    limit: int,
    min_observations: int,
    extra_markers: Iterable[str],
    dry_run: bool,
) -> tuple[int, int]:
    markers = build_project_markers(project_dir, extra_markers=extra_markers)
    candidates = load_session_candidates(db_path, hours=hours)
    matches = [
        row
        for row in candidates
        if is_substantive_session(row, min_observations=min_observations)
        and session_matches_project(row, markers)
    ]
    selected = matches[:limit]
    desired_paths: set[Path] = set()
    changed = 0
    for row in selected:
        note_path = vault_folder / "sessions" / session_filename(row)
        desired_paths.add(note_path)
        content = render_session_note(row, project_dir.name)
        if write_if_changed(note_path, content, dry_run=dry_run):
            changed += 1
    changed += prune_managed_notes(
        base_dir=vault_folder / "sessions",
        desired_paths=desired_paths,
        note_type="promoted-session-summary",
        dry_run=dry_run,
    )
    return len(selected), changed


def promote_reports(
    vault_folder: Path,
    project_dir: Path,
    reports_dir: Path,
    dry_run: bool,
) -> tuple[int, int]:
    report_paths = tracked_markdown_reports(project_dir=project_dir, reports_dir=reports_dir)
    desired_paths: set[Path] = set()
    changed = 0
    for report_path in report_paths:
        content = render_report_note(
            project_name=project_dir.name,
            report_path=report_path,
            content=read_committed_file(project_dir=project_dir, file_path=report_path),
        )
        note_path = vault_folder / "research" / report_path.relative_to(reports_dir)
        desired_paths.add(note_path)
        if write_if_changed(note_path, content, dry_run=dry_run):
            changed += 1
    changed += prune_managed_notes(
        base_dir=vault_folder / "research",
        desired_paths=desired_paths,
        note_type="promoted-report",
        dry_run=dry_run,
    )
    return len(report_paths), changed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Promote curated Cortex artifacts into Obsidian.")
    parser.add_argument("--project-dir", default=str(Path.cwd()), help="Project directory to match against the vault map.")
    parser.add_argument("--vault-dir", default=None, help="Override Obsidian vault root.")
    parser.add_argument("--db-path", default=str(default_db_path()), help="Path to cortex-observations.db.")
    parser.add_argument("--hours", type=int, default=168, help="Only consider session summaries newer than N hours.")
    parser.add_argument("--session-limit", type=int, default=5, help="Maximum number of session notes to promote.")
    parser.add_argument("--min-observations", type=int, default=5, help="Ignore tiny sessions below this observation count.")
    parser.add_argument("--project-marker", action="append", default=[], help="Additional marker to route summaries to this project.")
    parser.add_argument("--reports-dir", default=None, help="Override markdown report directory (default: <project>/reports).")
    parser.add_argument("--skip-sessions", action="store_true", help="Skip promoting session summaries.")
    parser.add_argument("--skip-reports", action="store_true", help="Skip promoting markdown reports.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print matched/changed counts and selected vault, write nothing.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_project_dir = Path(args.project_dir).expanduser().resolve()
    project_dir = resolve_project_root(input_project_dir)
    db_path = Path(args.db_path).expanduser().resolve()
    if args.reports_dir:
        raw_reports_dir = Path(args.reports_dir).expanduser()
        reports_dir = (project_dir / raw_reports_dir).resolve() if not raw_reports_dir.is_absolute() else raw_reports_dir.resolve()
    else:
        reports_dir = project_dir / "reports"
    vault_root = Path(args.vault_dir).expanduser().resolve() if args.vault_dir else None
    vault_folder = resolve_vault_folder(project_dir, vault_root=vault_root)
    if vault_folder is None:
        raise SystemExit(f"No Obsidian vault folder mapped for project: {project_dir}")
    if args.skip_sessions and args.skip_reports:
        raise SystemExit("Nothing to do: both --skip-sessions and --skip-reports were set.")
    if not args.skip_sessions and not db_path.exists():
        raise SystemExit(f"Cortex DB not found: {db_path}")

    promoted_total = 0
    changed_total = 0
    if not args.skip_sessions:
        promoted, changed = promote_sessions(
            vault_folder=vault_folder,
            project_dir=project_dir,
            db_path=db_path,
            hours=args.hours,
            limit=args.session_limit,
            min_observations=args.min_observations,
            extra_markers=args.project_marker,
            dry_run=args.dry_run,
        )
        promoted_total += promoted
        changed_total += changed
        print(f"session_notes: matched={promoted} changed={changed}")

    if not args.skip_reports:
        promoted, changed = promote_reports(
            vault_folder=vault_folder,
            project_dir=project_dir,
            reports_dir=reports_dir,
            dry_run=args.dry_run,
        )
        promoted_total += promoted
        changed_total += changed
        print(f"report_notes: matched={promoted} changed={changed}")

    mode = "dry-run" if args.dry_run else "write"
    print(f"{mode}: project={project_dir.name} vault={vault_folder} items={promoted_total} changed={changed_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
