#!/usr/bin/env python3
"""
map_projects.py — Scan ~/Projects/ git repos and generate/update Obsidian notes
in ~/Knowledge/projects/.

Usage:
    python3 map_projects.py [--dry-run] [--projects-dir PATH] [--vault-dir PATH] [--only REPO_NAME]
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore


# ---------------------------------------------------------------------------
# Slug aliases
# ---------------------------------------------------------------------------
SLUG_ALIASES = {
    "cami-chat": "cami",
    "ymi-website": "ymi",
    "MCP-Servers": "brokerbridge",
    "broker-bridge-retail": "broker-bridge-retail",
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def discover_repos(projects_dir: Path) -> List[Path]:
    """Return subdirs of projects_dir that contain a .git directory."""
    repos = []
    try:
        entries = list(projects_dir.iterdir())
    except PermissionError:
        return repos

    for entry in entries:
        # Skip hidden dirs, symlinks, files
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            continue
        if not entry.is_dir():
            continue
        if (entry / ".git").exists():
            repos.append(entry)

    return sorted(repos)


# ---------------------------------------------------------------------------
# Git metadata
# ---------------------------------------------------------------------------
def _run_git(args: List[str], cwd: Path) -> str:
    """Run a git command, return stdout or empty string on failure."""
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def get_git_metadata(repo: Path) -> dict:
    """Return git metadata dict for repo."""
    last_commit_full = _run_git(["git", "log", "-1", "--format=%ci"], repo)
    last_commit_date = last_commit_full[:10] if last_commit_full else ""

    last_commit_message = _run_git(["git", "log", "-1", "--format=%s"], repo)
    branch = _run_git(["git", "branch", "--show-current"], repo)
    remote_url = _run_git(["git", "remote", "get-url", "origin"], repo)

    return {
        "last_commit_date": last_commit_date,
        "last_commit_message": last_commit_message,
        "branch": branch,
        "remote_url": remote_url,
    }


# ---------------------------------------------------------------------------
# Stack detection
# ---------------------------------------------------------------------------
def _read_toml(path: Path) -> Optional[dict]:
    """Parse a TOML file, return dict or None on failure."""
    if tomllib is None:
        return None
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return None


def _framework_hints_from_deps(deps: List[str]) -> List[str]:
    """Given a list of dependency strings, return detected framework names."""
    frameworks = []
    dep_str = " ".join(deps).lower()

    mapping = [
        ("fastapi", "FastAPI"),
        ("flask", "Flask"),
        ("django", "Django"),
        ("torch", "PyTorch"),
        ("pytorch", "PyTorch"),
        ("pydantic", "Pydantic"),
        ("anthropic", "Anthropic SDK"),
        ("ib_insync", "ib_insync"),
    ]
    seen = set()
    for keyword, label in mapping:
        if keyword in dep_str and label not in seen:
            frameworks.append(label)
            seen.add(label)
    return frameworks


def _node_framework_hints(pkg: dict) -> List[str]:
    """Given parsed package.json dict, return detected framework/language names."""
    all_deps = {}
    all_deps.update(pkg.get("dependencies", {}))
    all_deps.update(pkg.get("devDependencies", {}))
    keys_str = " ".join(all_deps.keys()).lower()

    mapping = [
        ("react", "React"),
        ("next", "Next.js"),
        ("electron", "Electron"),
        ("express", "Express"),
        ("tauri", "Tauri"),
        ("vite", "Vite"),
        ("typescript", "TypeScript"),
    ]
    seen = set()
    frameworks = []
    for keyword, label in mapping:
        if keyword in keys_str and label not in seen:
            frameworks.append(label)
            seen.add(label)
    return frameworks


def detect_stack(repo: Path) -> dict:
    """Detect languages and frameworks from repo manifests."""
    languages = []
    frameworks = []
    lang_seen = set()

    def add_lang(lang: str) -> None:
        if lang not in lang_seen:
            languages.append(lang)
            lang_seen.add(lang)

    # 1. pyproject.toml → Python
    pyproject_path = repo / "pyproject.toml"
    if pyproject_path.exists():
        add_lang("Python")
        data = _read_toml(pyproject_path)
        if data is not None:
            deps = data.get("project", {}).get("dependencies", [])
            if isinstance(deps, list):
                frameworks.extend(_framework_hints_from_deps(deps))

    # 2. requirements.txt → Python (fallback for framework hints if no pyproject)
    req_path = repo / "requirements.txt"
    if req_path.exists():
        add_lang("Python")
        if not pyproject_path.exists():
            try:
                lines = req_path.read_text(encoding="utf-8", errors="replace").splitlines()
                frameworks.extend(_framework_hints_from_deps(lines))
            except Exception:
                pass

    # 3. package.json → Node/TypeScript
    pkg_path = repo / "package.json"
    if pkg_path.exists():
        add_lang("Node")
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
            frameworks.extend(_node_framework_hints(pkg))
        except Exception:
            pass

    # 4. Cargo.toml → Rust
    if (repo / "Cargo.toml").exists():
        add_lang("Rust")

    # 5. go.mod → Go
    if (repo / "go.mod").exists():
        add_lang("Go")

    # Deduplicate frameworks preserving order
    seen_fw = set()
    unique_frameworks = []
    for fw in frameworks:
        if fw not in seen_fw:
            unique_frameworks.append(fw)
            seen_fw.add(fw)

    return {"languages": languages, "frameworks": unique_frameworks}


# ---------------------------------------------------------------------------
# Project purpose
# ---------------------------------------------------------------------------
def _clean_purpose(text: str) -> str:
    """Strip markdown formatting from purpose strings."""
    # Remove bold/italic markers
    text = re.sub(r'\*+([^*]+)\*+', r'\1', text)
    # Remove inline code
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Remove image syntax ![alt](url)
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    # Remove link syntax [text](url) → text
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    return text.strip()


def get_project_purpose(repo: Path) -> str:
    """Extract project purpose from manifest or README."""
    # 1. pyproject.toml
    pyproject_path = repo / "pyproject.toml"
    if pyproject_path.exists():
        data = _read_toml(pyproject_path)
        if data is not None:
            desc = data.get("project", {}).get("description", "")
            if desc:
                return _clean_purpose(str(desc))

    # 2. package.json
    pkg_path = repo / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
            desc = pkg.get("description", "")
            if desc:
                return _clean_purpose(str(desc))
        except Exception:
            pass

    # 3. README.md — first non-empty line after skipping the title line
    readme_path = repo / "README.md"
    if readme_path.exists():
        try:
            lines = readme_path.read_text(encoding="utf-8", errors="replace").splitlines()
            skipped_title = False
            for line in lines:
                stripped = line.strip()
                if not skipped_title:
                    if stripped.startswith("#"):
                        skipped_title = True
                    continue
                # Strip blockquote prefix
                if stripped.startswith(">"):
                    stripped = stripped[1:].strip()
                if stripped and not stripped.startswith("#"):
                    return _clean_purpose(stripped[:200])
        except Exception:
            pass

    return ""


# ---------------------------------------------------------------------------
# Key files
# ---------------------------------------------------------------------------
def get_key_files(repo: Path) -> dict:
    """Return top-level and src/ directory listings."""
    SKIP = {
        "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".git"
    }

    # Top-level
    top_level = []
    try:
        entries = os.listdir(repo)
        for name in sorted(entries):
            if name.startswith("."):
                continue
            if name in SKIP:
                continue
            top_level.append(name)
    except Exception:
        pass
    top_level = top_level[:15]

    # src/
    src_entries = []
    src_path = repo / "src"
    if src_path.is_dir():
        try:
            src_entries = sorted(os.listdir(src_path))[:10]
        except Exception:
            pass

    return {"top_level": top_level, "src": src_entries}


# ---------------------------------------------------------------------------
# GitNexus
# ---------------------------------------------------------------------------
def get_gitnexus_data(repo: Path) -> Optional[dict]:
    """Parse GitNexus metadata from repo CLAUDE.md (first 10 lines)."""
    claude_md = repo / "CLAUDE.md"
    if not claude_md.exists():
        return None
    try:
        lines = claude_md.read_text(encoding="utf-8", errors="replace").splitlines()
        header = "\n".join(lines[:10])
        pattern = r'indexed by GitNexus as \*\*(\S+)\*\* \((\d+) symbols, (\d+) relationships'
        m = re.search(pattern, header)
        if m:
            return {
                "name": m.group(1),
                "symbols": int(m.group(2)),
                "relationships": int(m.group(3)),
            }
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Cross-references
# ---------------------------------------------------------------------------
def build_project_corpus(repos: List[Path]) -> set:
    """Return set of all repo dirnames."""
    return {repo.name for repo in repos}


def detect_cross_references(repo: Path, corpus: set, self_dirname: str) -> List[str]:
    """Find references to other repo names in README.md and CLAUDE.md."""
    text_parts = []

    readme = repo / "README.md"
    if readme.exists():
        try:
            text_parts.append(readme.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            pass

    claude_md = repo / "CLAUDE.md"
    if claude_md.exists():
        try:
            text_parts.append(claude_md.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            pass

    text = "\n".join(text_parts)

    found = set()
    for name in corpus:
        if name == self_dirname:
            continue
        if name in text:
            found.add(name)

    return sorted("[[" + name + "]]" for name in found)


# ---------------------------------------------------------------------------
# Note parsing
# ---------------------------------------------------------------------------
def parse_existing_note(path: Path) -> Tuple[Optional[str], str]:
    """
    Returns (frontmatter_block_or_None, body_string).
    frontmatter_block does NOT include the --- delimiters.
    Body includes everything after the closing --- delimiter.
    """
    if not path.exists():
        return (None, "")

    content = path.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)

    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                fm_block = "".join(lines[1:i])
                body = "".join(lines[i + 1:])
                if "generated_by: map_projects" in fm_block:
                    return (fm_block, body)
                else:
                    # Hand-written frontmatter — treat entire file as body
                    return (None, content)
        return (None, content)
    else:
        return (None, content)


# ---------------------------------------------------------------------------
# Frontmatter rendering
# ---------------------------------------------------------------------------
def _yaml_str(value: str) -> str:
    """Wrap a string value in double quotes, escaping internal double-quotes."""
    escaped = value.replace('"', '\\"')
    return f'"{escaped}"'


def render_frontmatter(data: dict) -> str:
    """Manually render YAML frontmatter block including --- delimiters."""
    lines = ["---"]
    lines.append("generated_by: map_projects")
    lines.append(f"generated_at: {data['generated_at']}")
    lines.append(f"name: {data['name']}")
    lines.append("body_managed: false")

    purpose = data.get("purpose", "")
    lines.append(f"purpose: {_yaml_str(purpose)}")

    # stack
    stack = data.get("stack", [])
    if stack:
        lines.append("stack:")
        for item in stack:
            lines.append(f"  - {item}")

    branch = data.get("branch", "")
    lines.append(f"branch: {branch}")

    last_commit_date = data.get("last_commit_date", "")
    if last_commit_date:
        lines.append(f"last_commit_date: {_yaml_str(last_commit_date)}")
    else:
        lines.append('last_commit_date: ""')

    last_commit_message = data.get("last_commit_message", "")
    lines.append(f"last_commit_message: {_yaml_str(last_commit_message)}")

    remote_url = data.get("remote_url", "")
    if remote_url:
        lines.append(f"remote_url: {remote_url}")

    gitnexus = data.get("gitnexus")
    if gitnexus:
        lines.append(f"gitnexus_symbols: {gitnexus['symbols']}")
        lines.append(f"gitnexus_relationships: {gitnexus['relationships']}")

    related = data.get("related_projects", [])
    if related:
        lines.append("related_projects:")
        for ref in related:
            lines.append(f"  - {_yaml_str(ref)}")

    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Body rendering
# ---------------------------------------------------------------------------
def render_body(data: dict) -> str:
    """Render the markdown body for a new note."""
    repo_name = data["name"]
    purpose = data.get("purpose", "") or "No description found."
    stack = data.get("stack", [])
    stack_str = ", ".join(stack) if stack else "Unknown"

    branch = data.get("branch", "")
    last_commit_date = data.get("last_commit_date", "")
    last_commit_message = data.get("last_commit_message", "")
    remote_url = data.get("remote_url", "") or "No remote"

    key_files = data.get("key_files", {})
    top_level = key_files.get("top_level", [])
    src = key_files.get("src", [])
    top_level_str = ", ".join(top_level) if top_level else "N/A"
    src_str = ", ".join(src) if src else "N/A"

    related = data.get("related_projects", [])
    related_str = ", ".join(related) if related else "None detected"

    gitnexus = data.get("gitnexus")
    if gitnexus:
        gitnexus_str = f"- Symbols: {gitnexus['symbols']}\n- Relationships: {gitnexus['relationships']}"
    else:
        gitnexus_str = "Not indexed."

    today = data.get("generated_at", "")[:10]

    body = (
        f"## {repo_name}\n"
        f"\n"
        f"### Purpose\n"
        f"{purpose}\n"
        f"\n"
        f"### Stack\n"
        f"{stack_str}\n"
        f"\n"
        f"### Git\n"
        f"- **Branch**: {branch}\n"
        f"- **Last commit**: {last_commit_date} — {last_commit_message}\n"
        f"- **Remote**: {remote_url}\n"
        f"\n"
        f"### Key Files\n"
        f"**Top-level**: {top_level_str}\n"
        f"**src/**: {src_str}\n"
        f"\n"
        f"### Related Projects\n"
        f"{related_str}\n"
        f"\n"
        f"### GitNexus\n"
        f"{gitnexus_str}\n"
        f"\n"
        f"_Last updated: {today}_\n"
    )
    return body


# ---------------------------------------------------------------------------
# Note composition
# ---------------------------------------------------------------------------
def compose_note(frontmatter_str: str, body: str) -> str:
    """Combine frontmatter and body into final note content."""
    if not body.startswith("\n"):
        return frontmatter_str + "\n" + body
    return frontmatter_str + "\n" + body


# ---------------------------------------------------------------------------
# INDEX.md update
# ---------------------------------------------------------------------------
def update_index(
    index_path: Path,
    all_slugs: List[Tuple[str, str]],
    dry_run: bool,
) -> None:
    """
    Update the projects section of INDEX.md.

    Strategy:
    - Lines starting with `- [projects/` are managed by this script.
    - All other lines (infrastructure, systems, trading, sessions, etc.) are preserved.
    - On first run: wrap existing project lines with sentinel markers.
    - On subsequent runs: replace content between sentinels.
    """
    start_marker = "<!-- map_projects:start -->"
    end_marker = "<!-- map_projects:end -->"

    # Build sorted entry lines
    sorted_slugs = sorted(all_slugs, key=lambda x: x[0])
    entry_lines = []
    for slug, purpose in sorted_slugs:
        display = purpose if purpose else slug
        entry_lines.append(f"- [projects/{slug}.md](projects/{slug}.md) — {display}")
    entries_block = "\n".join(entry_lines)

    if index_path.exists():
        current = index_path.read_text(encoding="utf-8")
    else:
        current = ""

    if start_marker in current:
        # Replace between existing markers
        pattern = re.compile(
            re.escape(start_marker) + r".*?" + re.escape(end_marker),
            re.DOTALL,
        )
        new_block = f"{start_marker}\n{entries_block}\n{end_marker}"
        new_content = pattern.sub(new_block, current)
    else:
        # No markers yet — find existing `- [projects/` lines and wrap them,
        # or if none found, append the block at the top of the file.
        lines = current.splitlines(keepends=True)
        project_start = None
        project_end = None
        for idx, line in enumerate(lines):
            if line.startswith("- [projects/"):
                if project_start is None:
                    project_start = idx
                project_end = idx

        if project_start is not None:
            # Replace the run of project lines with sentinel-wrapped new block
            before = "".join(lines[:project_start])
            after = "".join(lines[project_end + 1:])
            new_content = (
                before
                + start_marker + "\n"
                + entries_block + "\n"
                + end_marker + "\n"
                + after
            )
        else:
            # No existing project lines — prepend the block
            new_block = (
                start_marker + "\n"
                + entries_block + "\n"
                + end_marker + "\n"
            )
            new_content = new_block + current

    if dry_run:
        print(f"\n=== WOULD WRITE: {index_path} ===\n{new_content}\n")
    else:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(str(index_path) + ".tmp")
        tmp_path.write_text(new_content, encoding="utf-8")
        tmp_path.rename(index_path)


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------
def write_note(path: Path, content: str, dry_run: bool) -> None:
    """Write note atomically, or print in dry-run mode."""
    if dry_run:
        print(f"\n=== WOULD WRITE: {path} ===\n{content}\n")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(str(path) + ".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.rename(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    """Entry point for map_projects.py."""
    parser = argparse.ArgumentParser(
        description=(
            "Scan ~/Projects/ git repos and generate/update Obsidian notes "
            "in ~/Knowledge/projects/."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written, write nothing.",
    )
    parser.add_argument(
        "--projects-dir",
        default=str(Path.home() / "Projects"),
        help="Path to projects directory (default: ~/Projects).",
    )
    parser.add_argument(
        "--vault-dir",
        default=str(Path.home() / "Knowledge"),
        help="Path to Obsidian vault (default: ~/Knowledge).",
    )
    parser.add_argument(
        "--only",
        metavar="REPO_NAME",
        help="Process only one repo by dirname.",
    )
    args = parser.parse_args()

    projects_dir = Path(args.projects_dir).expanduser().resolve()
    vault_dir = Path(args.vault_dir).expanduser().resolve()

    if not projects_dir.exists():
        print(f"ERROR: projects-dir does not exist: {projects_dir}", file=sys.stderr)
        sys.exit(1)

    # Discover repos
    repos = discover_repos(projects_dir)

    if args.only:
        repos = [r for r in repos if r.name == args.only]
        if not repos:
            print(
                f"ERROR: No repo named '{args.only}' found in {projects_dir}",
                file=sys.stderr,
            )
            sys.exit(1)

    if not repos:
        print("No git repos found.", file=sys.stderr)
        sys.exit(0)

    # Build full corpus for cross-reference detection (always use all repos)
    all_repos_for_corpus = discover_repos(projects_dir)
    corpus = build_project_corpus(all_repos_for_corpus)

    now_str = datetime.now().isoformat(timespec="seconds")

    errors = []
    all_slugs: List[Tuple[str, str]] = []
    written_count = 0

    for repo in repos:
        try:
            slug = SLUG_ALIASES.get(repo.name, repo.name)
            note_path = vault_dir / "projects" / f"{slug}.md"

            # Collect metadata
            git_meta = get_git_metadata(repo)
            stack = detect_stack(repo)
            purpose = get_project_purpose(repo)
            key_files = get_key_files(repo)
            gitnexus = get_gitnexus_data(repo)
            cross_refs = detect_cross_references(repo, corpus, repo.name)

            # Combined stack: languages first, then frameworks
            combined_stack = stack["languages"] + stack["frameworks"]

            # Assemble data dict for rendering
            data = {
                "name": repo.name,
                "generated_at": now_str,
                "purpose": purpose,
                "stack": combined_stack,
                "branch": git_meta["branch"],
                "last_commit_date": git_meta["last_commit_date"],
                "last_commit_message": git_meta["last_commit_message"],
                "remote_url": git_meta["remote_url"],
                "gitnexus": gitnexus,
                "related_projects": cross_refs,
                "key_files": key_files,
            }

            # Parse existing note
            existing_fm, existing_body = parse_existing_note(note_path)

            # Render frontmatter (always regenerated)
            fm_str = render_frontmatter(data)

            # Body: preserve existing if present, otherwise render fresh
            if existing_body.strip():
                body = existing_body
            else:
                body = render_body(data)

            # Compose and write
            note_content = compose_note(fm_str, body)
            write_note(note_path, note_content, args.dry_run)

            all_slugs.append((slug, purpose))
            written_count += 1

        except Exception as e:
            print(f"WARNING: skipped {repo.name}: {e}", file=sys.stderr)
            errors.append(repo.name)

    # Update INDEX.md with all processed slugs
    if all_slugs:
        index_path = vault_dir / "INDEX.md"
        try:
            update_index(index_path, all_slugs, args.dry_run)
        except Exception as e:
            print(f"WARNING: failed to update INDEX.md: {e}", file=sys.stderr)
            errors.append("INDEX.md")

    # Summary
    skipped = len(errors)
    action = "Would write" if args.dry_run else "Wrote"
    print(f"{action} {written_count} notes, skipped {skipped} (errors).")

    if errors:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
