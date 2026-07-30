"""Plan (.plan.md) discovery, export, and restore for Cursor Plan mode.

Cursor stores plan metadata in global ItemTable key ``composer.planRegistry``
and plan bodies as markdown files (typically ``~/.cursor/plans/*.plan.md`` on
the environment that hosts the agent — WSL home for Remote-WSL).

cursaves embeds linked plan bodies into conversation snapshots and restores
them next to the target workspace, rewriting ``planRegistry`` URIs for the
local machine. Content is stored in plaintext in the sync repo (same trust
model as chat snapshots).
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

from . import db, paths


_PLAN_REGISTRY_KEY = "composer.planRegistry"
_HOME_PLANS_RE = re.compile(
    r"^(?:/(?:home|Users)/[^/]+|/[A-Za-z]:/Users/[^/]+|[A-Za-z]:/Users/[^/]+)"
    r"/\.cursor/plans/[^/]+\.plan\.md$",
    re.IGNORECASE,
)


def load_plan_registry(cdb: Optional[db.CursorDB] = None) -> dict[str, dict]:
    """Return composer.planRegistry mapping, or {} if absent."""
    own = cdb is None
    if own:
        gdb = paths.get_global_db_path()
        if not gdb.exists():
            return {}
        cdb = db.CursorDB(gdb)
    try:
        data = cdb.get_json(_PLAN_REGISTRY_KEY, table="ItemTable")
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, dict)}
        return {}
    finally:
        if own:
            cdb.close()


def save_plan_registry(registry: dict[str, dict], cdb: db.CursorDB) -> None:
    """Write the full plan registry dict to ItemTable."""
    cdb.write_json(_PLAN_REGISTRY_KEY, registry, table="ItemTable")


def plans_linked_to_composer(registry: dict[str, dict], composer_id: str) -> list[dict]:
    """Return registry entries that reference ``composer_id``."""
    linked = []
    for plan_id, entry in registry.items():
        if _entry_links_composer(entry, composer_id):
            row = dict(entry)
            row.setdefault("id", plan_id)
            linked.append(row)
    return linked


def _entry_links_composer(entry: dict, composer_id: str) -> bool:
    if entry.get("createdBy") == composer_id:
        return True
    for key in ("editedBy", "referencedBy"):
        val = entry.get(key)
        if isinstance(val, list) and composer_id in val:
            return True
    built = entry.get("builtBy")
    if isinstance(built, dict) and composer_id in built:
        return True
    return False


def is_home_plans_path(posix_path: str) -> bool:
    """True if path looks like ``{home}/.cursor/plans/*.plan.md`` (not project)."""
    p = posix_path.replace("\\", "/")
    if not p.startswith("/"):
        # Windows drive form already matched by regex alternative
        pass
    return bool(_HOME_PLANS_RE.match(p)) or bool(
        re.match(
            r"^[A-Za-z]:/Users/[^/]+/\.cursor/plans/[^/]+\.plan\.md$",
            p,
            re.IGNORECASE,
        )
    )


def uri_to_readable_path(uri: dict) -> str:
    """POSIX-ish path from a planRegistry uri object."""
    if not isinstance(uri, dict):
        return ""
    path = uri.get("path") or ""
    if path:
        return path.replace("\\", "/")
    fs = uri.get("fsPath") or ""
    return fs.replace("\\", "/")


def resolve_plan_file_for_read(uri: dict) -> Optional[Path]:
    """Map a planRegistry URI to a Path readable from this OS (Windows/WSL)."""
    if not isinstance(uri, dict):
        return None
    scheme = uri.get("scheme") or "file"
    path = uri_to_readable_path(uri)
    if not path:
        return None

    if scheme == "vscode-remote":
        authority = unquote(uri.get("authority") or "")
        if authority.lower().startswith("wsl+"):
            distro = authority.split("+", 1)[1]
            # Prefer \\wsl$\Distro\... (works from Windows Python)
            unc = "\\\\wsl$\\" + distro + path.replace("/", "\\")
            p = Path(unc)
            if p.is_file():
                return p
            # Fallback: wsl.localhost
            unc2 = "\\\\wsl.localhost\\" + distro + path.replace("/", "\\")
            p2 = Path(unc2)
            if p2.is_file():
                return p2
            return p if p.exists() else p2
        # Non-WSL remote: not readable from Windows host directly
        return None

    # file:// or bare path
    if platform.system() == "Windows" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return Path(path)


def collect_plans_for_composer(
    composer_id: str,
    cdb: Optional[db.CursorDB] = None,
) -> list[dict]:
    """Build snapshot ``plans`` entries for a composer (content + metadata)."""
    registry = load_plan_registry(cdb)
    linked = plans_linked_to_composer(registry, composer_id)
    results: list[dict] = []
    for entry in linked:
        plan_id = entry.get("id") or ""
        uri = entry.get("uri") if isinstance(entry.get("uri"), dict) else {}
        path = uri_to_readable_path(uri)
        file_path = resolve_plan_file_for_read(uri)
        content = None
        read_error = None
        if file_path is not None:
            try:
                content = file_path.read_text(encoding="utf-8")
            except OSError as e:
                read_error = str(e)
        else:
            read_error = "plan file not readable from this host"

        if content is None:
            print(
                f"  Warning: plan {plan_id!r} linked but unread ({read_error})",
                file=sys.stderr,
            )
            continue

        results.append({
            "id": plan_id,
            "name": entry.get("name"),
            "content": content,
            "tier": "home" if is_home_plans_path(path) else "project",
            "sourceUri": uri,
            "sourcePath": path,
            "createdBy": entry.get("createdBy"),
            "editedBy": list(entry.get("editedBy") or []),
            "referencedBy": list(entry.get("referencedBy") or []),
            "builtBy": entry.get("builtBy") if isinstance(entry.get("builtBy"), dict) else {},
            "lastUpdatedAt": entry.get("lastUpdatedAt"),
            "createdAt": entry.get("createdAt"),
        })
    return results


def _infer_unix_home(project_path: str) -> Optional[str]:
    """Infer /home/user from a remote project path."""
    canon = paths.canonicalize_project_path(project_path)
    m = re.match(r"^(/home/[^/]+)", canon.replace("\\", "/"))
    if m:
        return m.group(1)
    m = re.match(r"^(/Users/[^/]+)", canon.replace("\\", "/"))
    if m:
        return m.group(1)
    return None


def _build_plan_uri(
    *,
    posix_path: str,
    scheme: str,
    authority: Optional[str] = None,
) -> dict:
    """Build a planRegistry-style uri object."""
    path = posix_path.replace("\\", "/")
    if not path.startswith("/") and not (len(path) > 1 and path[1] == ":"):
        path = "/" + path
    uri: dict[str, Any] = {"$mid": 1, "path": path, "scheme": scheme}
    if scheme == "vscode-remote" and authority:
        auth = authority.lower()
        uri["authority"] = auth
        uri["_sep"] = 1
        uri["fsPath"] = path.replace("/", "\\")
        ext_auth = auth.replace("+", "%2B")
        uri["external"] = f"vscode-remote://{ext_auth}{path}"
    else:
        # file scheme
        fs = path
        if platform.system() == "Windows" and len(fs) > 2 and fs[0] == "/" and fs[2] == ":":
            fs = fs[1:]
        if platform.system() == "Windows":
            uri["fsPath"] = fs.replace("/", "\\")
        else:
            uri["fsPath"] = fs
        # external file URI
        ext_path = path if path.startswith("/") else "/" + path
        uri["external"] = "file://" + ext_path
    return uri


def _detect_wsl_target(
    target_workspace_dir: Path,
    target_project_path: str,
    wi_uri: dict,
) -> Optional[tuple[str, str]]:
    """If the workspace is WSL-backed, return ``(distro, canonical_posix_path)``.

    Handles both ``vscode-remote://wsl+…`` and ``file://wsl.localhost/…`` /
    ``\\\\wsl$\\…`` forms (the latter appear as scheme=file after
    ``_build_workspace_identifier``).
    """
    candidates: list[str] = []
    ext = wi_uri.get("external")
    if isinstance(ext, str) and ext:
        candidates.append(ext)
    scheme = wi_uri.get("scheme") or ""
    auth = wi_uri.get("authority") or ""
    if scheme == "vscode-remote" and auth:
        path = wi_uri.get("path") or ""
        ext_auth = str(auth).replace("+", "%2B")
        candidates.append(f"vscode-remote://{ext_auth}{path}")

    ws_json = target_workspace_dir / "workspace.json"
    if ws_json.exists():
        try:
            data = json.loads(ws_json.read_text(encoding="utf-8"))
            folder_uri = data.get("folder") or data.get("workspace") or ""
            if folder_uri:
                candidates.append(folder_uri)
        except (OSError, json.JSONDecodeError):
            pass
    if target_project_path:
        candidates.append(target_project_path)

    for raw in candidates:
        if not isinstance(raw, str) or not raw:
            continue
        s = raw.replace("\\", "/")

        if s.startswith("vscode-remote://"):
            parts = s.split("/", 3)
            authority = unquote(parts[2]) if len(parts) > 2 else ""
            # Expect ``wsl+Distro`` (after unquote of ``wsl%2BDistro``)
            if authority.lower().startswith("wsl+") and "+" in authority:
                distro = unquote(authority.split("+", 1)[1]).strip()
                if distro:
                    posix = paths.canonicalize_project_path(s)
                    return distro, posix

        m = re.match(
            r"^(?:file://)?/*wsl(?:\.localhost|\$)/+([^/]+)(?:/+(.*))?$",
            s,
            flags=re.IGNORECASE,
        )
        if m:
            distro = m.group(1)
            rest = m.group(2) or ""
            posix = paths.canonicalize_project_path(raw)
            if not posix or posix == raw.replace("\\", "/"):
                posix = ("/" + rest) if rest and not rest.startswith("/") else (rest or "/")
            return distro, posix

    return None


def resolve_plan_destination(
    plan: dict,
    target_project_path: str,
    target_workspace_dir: Path,
) -> tuple[Path, dict]:
    """Choose write Path + registry uri for a plan on this machine.

    Returns (filesystem_path_for_write, uri_for_registry).
    """
    plan_id = plan.get("id") or "unknown"
    filename = f"{plan_id}.plan.md"
    tier = plan.get("tier") or "home"

    from .importer import _build_workspace_identifier

    wi = _build_workspace_identifier(target_workspace_dir)
    uri = (wi or {}).get("uri") or {}
    wsl = _detect_wsl_target(target_workspace_dir, target_project_path, uri)

    if tier == "project":
        if wsl:
            distro, proj_posix = wsl
            remote_file = f"{proj_posix.rstrip('/')}/.cursor/plans/{filename}"
            write_path = Path("\\\\wsl$\\" + distro + remote_file.replace("/", "\\"))
            reg_uri = _build_plan_uri(
                posix_path=remote_file,
                scheme="vscode-remote",
                authority=f"wsl+{distro}",
            )
            return write_path, reg_uri
        # Local project on this host
        proj = Path(target_project_path)
        write_path = proj / ".cursor" / "plans" / filename
        reg_uri = _build_plan_uri(
            posix_path=str(write_path).replace("\\", "/"), scheme="file"
        )
        if platform.system() == "Windows":
            reg_uri["path"] = "/" + str(write_path).replace("\\", "/")
            reg_uri["fsPath"] = str(write_path)
            reg_uri["external"] = "file:///" + str(write_path).replace("\\", "/")
        return write_path, reg_uri

    # Home-tier plans
    if wsl:
        distro, proj_posix = wsl
        home = _infer_unix_home(proj_posix) or _infer_unix_home(target_project_path)
        if not home:
            src = (plan.get("sourcePath") or "").replace("\\", "/")
            m = re.match(r"^(/home/[^/]+)", src)
            home = m.group(1) if m else "/home/" + os.environ.get("USERNAME", "user").lower()
        remote_file = f"{home}/.cursor/plans/{filename}"
        write_path = Path("\\\\wsl$\\" + distro + remote_file.replace("/", "\\"))
        reg_uri = _build_plan_uri(
            posix_path=remote_file,
            scheme="vscode-remote",
            authority=f"wsl+{distro}",
        )
        return write_path, reg_uri

    # Local Windows / macOS / Linux home
    write_path = Path.home() / ".cursor" / "plans" / filename
    if platform.system() == "Windows":
        reg_uri = {
            "$mid": 1,
            "fsPath": str(write_path),
            "path": "/" + str(write_path).replace("\\", "/"),
            "scheme": "file",
            "external": "file:///" + str(write_path).replace("\\", "/"),
        }
    else:
        reg_uri = _build_plan_uri(posix_path=str(write_path), scheme="file")
    return write_path, reg_uri


def restore_plans_for_composer(
    plans: list[dict],
    composer_id: str,
    target_project_path: str,
    target_workspace_dir: Path,
    cdb: db.CursorDB,
) -> int:
    """Write plan files and upsert planRegistry entries. Returns plans written."""
    if not plans:
        return 0

    registry = load_plan_registry(cdb)
    written = 0

    for plan in plans:
        if not isinstance(plan, dict):
            continue
        plan_id = plan.get("id")
        content = plan.get("content")
        if not plan_id or content is None:
            continue

        try:
            dest, reg_uri = resolve_plan_destination(
                plan, target_project_path, target_workspace_dir
            )
        except Exception as e:
            print(f"  Warning: plan {plan_id}: resolve failed: {e}", file=sys.stderr)
            continue

        kept_local = False
        local_mtime_ms = 0.0
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Don't clobber newer (or unknown-timestamp) local plan bodies
            if dest.is_file():
                try:
                    existing = dest.read_text(encoding="utf-8")
                    local_mtime_ms = dest.stat().st_mtime * 1000
                    if existing == content:
                        pass
                    else:
                        incoming_ts = plan.get("lastUpdatedAt") or 0
                        try:
                            incoming_ts = int(incoming_ts)
                        except (TypeError, ValueError):
                            incoming_ts = 0
                        # No incoming timestamp → keep local when content differs.
                        # With timestamp → keep local only if mtime is newer.
                        if not incoming_ts or local_mtime_ms > incoming_ts:
                            print(
                                f"  Plan {plan_id}: keeping local body "
                                f"(incoming_ts={incoming_ts or 'none'})",
                            )
                            content = existing
                            kept_local = True
                except OSError:
                    pass
            dest.write_text(content, encoding="utf-8")
            if not local_mtime_ms and dest.is_file():
                try:
                    local_mtime_ms = dest.stat().st_mtime * 1000
                except OSError:
                    pass
        except OSError as e:
            print(f"  Warning: plan {plan_id}: write failed: {e}", file=sys.stderr)
            continue

        # Merge registry entry
        prev = dict(registry.get(plan_id) or {})
        edited = list(prev.get("editedBy") or plan.get("editedBy") or [])
        referenced = list(prev.get("referencedBy") or plan.get("referencedBy") or [])
        for lst in (edited, referenced):
            if composer_id not in lst:
                lst.append(composer_id)
        created_by = prev.get("createdBy") or plan.get("createdBy") or composer_id
        built = prev.get("builtBy") if isinstance(prev.get("builtBy"), dict) else {}
        if isinstance(plan.get("builtBy"), dict):
            for k, v in plan["builtBy"].items():
                built.setdefault(k, v)

        incoming_ts = plan.get("lastUpdatedAt") or 0
        try:
            incoming_ts = int(incoming_ts)
        except (TypeError, ValueError):
            incoming_ts = 0
        prev_ts = prev.get("lastUpdatedAt") or 0
        try:
            prev_ts = int(prev_ts)
        except (TypeError, ValueError):
            prev_ts = 0

        if kept_local:
            # Do not let a stale snapshot timestamp regress the registry
            last_updated = max(prev_ts, int(local_mtime_ms) if local_mtime_ms else 0)
        else:
            last_updated = incoming_ts or prev_ts or None

        registry[plan_id] = {
            "id": plan_id,
            "name": plan.get("name") or prev.get("name") or plan_id,
            "uri": reg_uri,
            "createdBy": created_by,
            "editedBy": edited,
            "referencedBy": referenced,
            "builtBy": built,
            "lastUpdatedAt": last_updated,
            "createdAt": plan.get("createdAt") or prev.get("createdAt"),
        }
        written += 1
        print(f"  Plan restored: {plan_id} → {dest}")

    if written:
        save_plan_registry(registry, cdb)
    return written
