#!/usr/bin/env python3
"""Shared helpers for selective Obsidian integration."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_OBSIDIAN_VAULT = (
    Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "Cameron"
)

# Checked in order — first match wins.
PROJECT_VAULT_MAP: list[tuple[str, str]] = [
    ("brokerbridge-retail-hermes", "BrokerBridge"),
    ("broker-bridge-retail", "BrokerBridge"),
    ("MCP-Servers/brokerbridge", "BrokerBridgeMCP"),
    ("brokerbridge", "BrokerBridgeMCP"),
    ("openclaw", "OpenClaw"),
    ("matrix-lstm", "MatrixLSTM"),
    ("moltytrades", "MoltyTrades"),
    ("CLIProxyAPI", "CLIProxyAPI"),
    ("ymi-website", "YMIWebsite"),
    ("llm-cortex", "LLMCortex"),
]


def resolve_vault_match(project_dir: str | Path) -> tuple[Optional[str], Optional[str]]:
    """Return (matched_pattern, vault_name) for a project directory."""
    project_path = resolve_project_root(Path(project_dir).expanduser().resolve())
    project_parts = [part.lower() for part in project_path.parts]
    for pattern, vault_name in PROJECT_VAULT_MAP:
        pattern_parts = [part.lower() for part in pattern.split("/")]
        if len(pattern_parts) == 1 and project_path.name.lower() == pattern_parts[0]:
            return pattern, vault_name
        if len(pattern_parts) > 1 and project_parts[-len(pattern_parts):] == pattern_parts:
            return pattern, vault_name
    return None, None


def resolve_project_root(project_dir: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return project_dir
    if result.returncode != 0:
        return project_dir
    root = result.stdout.strip()
    return Path(root).expanduser().resolve() if root else project_dir


def resolve_vault_folder(project_dir: str | Path, vault_root: Optional[Path] = None) -> Optional[Path]:
    """Return the Obsidian vault subfolder for the given project directory, or None."""
    _, vault_name = resolve_vault_match(project_dir)
    if vault_name is None:
        return None
    root = (vault_root or DEFAULT_OBSIDIAN_VAULT).expanduser()
    folder = root / vault_name
    if folder.exists():
        return folder
    return None


def build_project_markers(
    project_dir: str | Path,
    extra_markers: Optional[Iterable[str]] = None,
) -> list[str]:
    """Build conservative markers for matching Cortex summaries to a project.

    Strong markers only. Avoid generic tokens that would over-match unrelated
    sessions across the user's machine.
    """

    project_path = Path(project_dir).expanduser().resolve()
    pattern, vault_name = resolve_vault_match(project_path)
    raw_markers = {
        project_path.name,
        str(project_path),
        f"/Projects/{project_path.name}",
        f"-Users-cameronbennion-Projects-{project_path.name}",
    }
    if pattern:
        raw_markers.add(pattern)
    if vault_name:
        raw_markers.add(vault_name)
    # Hyphen-stripped form catches renderings like "LLMCortex".
    raw_markers.add(project_path.name.replace("-", ""))

    if extra_markers:
        raw_markers.update(m for m in extra_markers if m)

    markers = []
    seen = set()
    for marker in raw_markers:
        normalized = marker.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        markers.append(normalized)
    return sorted(markers, key=len, reverse=True)


def read_text_with_timeout(path: Path, timeout_seconds: int = 5) -> Optional[str]:
    """Read an Obsidian file with a hard timeout.

    iCloud-backed vault paths can occasionally block indefinitely on reads.
    Use a subprocess so the caller can fail fast instead of wedging the whole
    process.
    """

    if not path.exists():
        return None

    try:
        result = subprocess.run(
            ["/bin/cat", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Timed out reading Obsidian note: {path}") from exc
    except Exception as exc:
        raise RuntimeError(f"Unable to read Obsidian note: {path}") from exc

    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown read error"
        raise RuntimeError(f"Unable to read Obsidian note {path}: {stderr}")
    return result.stdout
