"""Sync personal and project Cursor agent config via ~/.cursaves/agent-config/.

Covers skills, rules, commands, agents, and project AGENTS.md.
Skips skills-cursor, MCP, hooks, and git-tracked project paths.
"""

from __future__ import annotations

import fnmatch
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

from . import paths

_PERSONAL_DIRS = ("skills", "rules", "commands", "agents")
_PROJECT_DIRS = ("skills", "rules", "commands", "agents")
_SKIP_NAME_GLOBS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*credentials*",
)
_logged_git_skip: set[str] = set()
_wsl_log_done = False


def get_agent_config_dir() -> Path:
    d = paths.get_sync_dir() / "agent-config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_dangerous_name(name: str) -> bool:
    lower = name.lower()
    return any(fnmatch.fnmatch(lower, pat.lower()) for pat in _SKIP_NAME_GLOBS)


def _should_skip_file(path: Path) -> bool:
    return _is_dangerous_name(path.name)


def _files_equal(a: Path, b: Path) -> bool:
    try:
        if not a.is_file() or not b.is_file():
            return False
        if a.stat().st_size != b.stat().st_size:
            return False
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _copy_file(src: Path, dest: Path) -> bool:
    """Copy src→dest if bytes differ. Returns True if wrote."""
    if _should_skip_file(src):
        return False
    if dest.exists() and _files_equal(src, dest):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    if root.is_file():
        if not _should_skip_file(root):
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # Never descend into skills-cursor
        dirnames[:] = [d for d in dirnames if d != "skills-cursor" and not _is_dangerous_name(d)]
        for name in filenames:
            if _is_dangerous_name(name):
                continue
            yield Path(dirpath) / name


def _git_is_work_tree(project_path: Path) -> bool:
    try:
        r = subprocess.run(
            ["git", "-C", str(project_path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return r.returncode == 0 and (r.stdout or "").strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


def _git_tracked(project_path: Path, rel_posix: str) -> bool:
    """True if path is tracked in the project git repo."""
    try:
        r = subprocess.run(
            ["git", "-C", str(project_path), "ls-files", "--error-unmatch", "--", rel_posix],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _log_git_skip(project_id: str) -> None:
    if project_id in _logged_git_skip:
        return
    _logged_git_skip.add(project_id)
    print(
        f"  Agent-config: project '{project_id}' has git-tracked paths — "
        "skipping those (repo git is source of truth)",
        file=sys.stderr,
    )


def _personal_cursor_home() -> Path:
    return Path.home() / ".cursor"


def _log_wsl_skip(reason: str, *, action: str = "mirror") -> None:
    """Log a one-shot WSL skip. action is 'export' or 'mirror'."""
    global _wsl_log_done
    if _wsl_log_done:
        return
    _wsl_log_done = True
    print(f"  Agent-config: WSL personal {action} skipped ({reason})", file=sys.stderr)


def _detect_wsl_personal_cursor() -> Optional[Path]:
    """Windows → \\\\wsl$\\Distro\\home\\user\\.cursor for the default WSL distro only.

    Uses bare ``wsl`` (default distro); does not enumerate other distros.
    """
    if platform.system() != "Windows":
        return None
    try:
        home = subprocess.run(
            ["wsl", "-e", "bash", "-lc", 'printf %s "$HOME"'],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
        distro = subprocess.run(
            ["wsl", "-e", "bash", "-lc", 'printf %s "$WSL_DISTRO_NAME"'],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
        if home.returncode != 0 or not (home.stdout or "").strip():
            return None
        d = (distro.stdout or "").strip()
        if not d:
            # Do not invent a default distro name — treat as undetectable.
            return None
        h = home.stdout.strip().replace("/", "\\").lstrip("\\")
        wsl_home = Path("\\\\wsl$\\" + d) / h
        try:
            if not wsl_home.exists():
                return None
        except OSError:
            return None
        return wsl_home / ".cursor"
    except (OSError, subprocess.TimeoutExpired):
        _log_wsl_skip("WSL unavailable")
        return None


def _sync_tree(src_root: Path, dest_root: Path, *, relative_from: Path) -> int:
    """Copy files under src_root into dest_root preserving relpath from relative_from."""
    written = 0
    if not src_root.exists():
        return 0
    for src in _iter_files(src_root):
        try:
            rel = src.relative_to(relative_from)
        except ValueError:
            rel = src.name
        dest = dest_root / rel
        if _copy_file(src, dest):
            written += 1
    return written


def export_personal() -> int:
    """Copy ~/.cursor/{skills,rules,commands,agents} → sync personal/.

    On Windows, also export from the default WSL distro's ``~/.cursor`` when
    detectable. Windows runs first; on path conflicts WSL always wins
    (order-based overwrite, not mtime). WSL export is best-effort and never
    aborts the Windows export.
    """
    cursor = _personal_cursor_home()
    dest_base = get_agent_config_dir() / "personal"
    n = 0
    for name in _PERSONAL_DIRS:
        src = cursor / name
        if not src.exists():
            continue
        n += _sync_tree(src, dest_base / name, relative_from=src)

    # WSL export is best-effort — never abort Windows personal export.
    # Windows first, then WSL so WSL always wins on conflicting paths.
    wsl = _detect_wsl_personal_cursor()
    if wsl is not None:
        try:
            for name in _PERSONAL_DIRS:
                src = wsl / name
                if not src.exists():
                    continue
                n += _sync_tree(src, dest_base / name, relative_from=src)
        except Exception:
            _log_wsl_skip("export path error", action="export")
    return n


def import_personal() -> int:
    """Copy sync personal/ → ~/.cursor/… and optional WSL home."""
    src_base = get_agent_config_dir() / "personal"
    if not src_base.exists():
        return 0
    n = 0
    for name in _PERSONAL_DIRS:
        src = src_base / name
        if not src.exists():
            continue
        n += _sync_tree(src, _personal_cursor_home() / name, relative_from=src)

    # WSL mirror is best-effort — never abort Windows personal import.
    wsl = _detect_wsl_personal_cursor()
    if wsl is not None:
        try:
            for name in _PERSONAL_DIRS:
                src = src_base / name
                if not src.exists():
                    continue
                n += _sync_tree(src, wsl / name, relative_from=src)
        except OSError:
            _log_wsl_skip("mirror path error", action="mirror")
    return n


def _project_rel_candidates(project_path: Path) -> list[tuple[str, Path]]:
    """Return (rel_posix, absolute_path) candidates for project agent config."""
    out: list[tuple[str, Path]] = []
    cursor = project_path / ".cursor"
    for name in _PROJECT_DIRS:
        p = cursor / name
        if p.exists():
            out.append((f".cursor/{name}", p))
    agents_md = project_path / "AGENTS.md"
    if agents_md.is_file():
        out.append(("AGENTS.md", agents_md))
    return out


def export_project(project_path: str) -> int:
    """Export untracked/ignored project agent-config into sync projects/<id>/."""
    proj = Path(project_path)
    try:
        if not proj.exists():
            return 0
    except OSError:
        return 0
    project_id = paths.get_project_identifier(str(proj))
    dest_base = get_agent_config_dir() / "projects" / project_id
    is_git = _git_is_work_tree(proj)
    n = 0
    skipped_tracked = False

    for rel, src in _project_rel_candidates(proj):
        if is_git:
            # Directory: skip individual tracked files; copy only untracked
            if src.is_dir():
                for f in _iter_files(src):
                    try:
                        rel_f = f.relative_to(proj).as_posix()
                    except ValueError:
                        continue
                    if _git_tracked(proj, rel_f):
                        skipped_tracked = True
                        continue
                    dest = dest_base / Path(rel_f)
                    # store under projects/id/.cursor/... or AGENTS.md
                    if _copy_file(f, dest):
                        n += 1
                continue
            if _git_tracked(proj, rel):
                skipped_tracked = True
                continue
        # not git, or untracked file
        if src.is_file():
            if _copy_file(src, dest_base / rel):
                n += 1
        elif src.is_dir():
            n += _sync_tree(src, dest_base / rel, relative_from=src)

    if skipped_tracked:
        _log_git_skip(project_id)
    return n


def _is_allowed_project_import_rel(rel: str) -> bool:
    """Only restore AGENTS.md (root) and .cursor/{skills,rules,commands,agents}/…"""
    if rel == "AGENTS.md":
        return True
    for name in _PROJECT_DIRS:
        prefix = f".cursor/{name}"
        if rel == prefix or rel.startswith(prefix + "/"):
            return True
    return False


def import_project(project_path: str) -> int:
    """Restore sync projects/<id>/ into project_path, never overwriting git-tracked files."""
    proj = Path(project_path)
    try:
        if not proj.is_dir():
            return 0
    except OSError:
        return 0
    project_id = paths.get_project_identifier(str(proj))
    src_base = get_agent_config_dir() / "projects" / project_id
    if not src_base.exists():
        return 0
    is_git = _git_is_work_tree(proj)
    n = 0
    for src in _iter_files(src_base):
        try:
            rel = src.relative_to(src_base).as_posix()
        except ValueError:
            continue
        if not _is_allowed_project_import_rel(rel):
            print(
                f"  Agent-config: skipping disallowed path '{rel}' for '{project_id}'",
                file=sys.stderr,
            )
            continue
        if is_git and _git_tracked(proj, rel):
            _log_git_skip(project_id)
            continue
        dest = proj / rel
        if _copy_file(src, dest):
            n += 1
    return n


def export_agent_config(project_paths: Optional[list[str]] = None) -> int:
    """Export personal + projects. None = all known workspace project paths."""
    n = export_personal()
    if project_paths is None:
        project_paths = []
        try:
            for ws in paths.list_all_workspaces():
                p = ws.get("path") or ""
                if p:
                    project_paths.append(p)
        except Exception:
            pass
    for pp in project_paths:
        if pp:
            n += export_project(pp)
    if n:
        print(f"  Agent-config: exported {n} file(s)")
    return n


def import_agent_config(project_paths: Optional[list[str]] = None) -> int:
    """Import personal + projects. If project_paths is None, import all project ids in sync tree."""
    n = import_personal()
    projects_root = get_agent_config_dir() / "projects"
    if project_paths is not None:
        for pp in project_paths:
            if pp:
                n += import_project(pp)
    elif projects_root.exists():
        # Map projectIdentifier → best local path via workspace list
        id_to_path = _project_id_to_local_paths()
        for child in projects_root.iterdir():
            if not child.is_dir():
                continue
            pid = child.name
            locals_ = id_to_path.get(pid) or []
            if not locals_:
                continue
            for lp in locals_:
                n += import_project(lp)
    if n:
        print(f"  Agent-config: imported {n} file(s)")
    return n


def _project_id_to_local_paths() -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    try:
        for ws in paths.list_all_workspaces():
            p = ws.get("path") or ""
            if not p:
                continue
            pid = paths.get_project_identifier(p)
            mapping.setdefault(pid, [])
            if p not in mapping[pid]:
                mapping[pid].append(p)
    except Exception:
        pass
    return mapping
