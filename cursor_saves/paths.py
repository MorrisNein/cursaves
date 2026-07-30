"""Platform detection and Cursor storage path resolution."""

import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import unquote


def get_cursor_user_dir() -> Path:
    """Return the Cursor User data directory for the current platform.

    macOS:  ~/Library/Application Support/Cursor/User
    Linux:  ~/.config/Cursor/User
    Windows: %APPDATA%\\Cursor\\User
    """
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Cursor" / "User"
    elif system == "Linux":
        base = Path.home() / ".config" / "Cursor" / "User"
    elif system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Cursor" / "User"
    else:
        print(
            f"Error: Unsupported platform '{system}'.\n"
            f"cursaves supports macOS, Linux, and Windows.\n"
            f"On macOS, Cursor data is at ~/Library/Application Support/Cursor/User/\n"
            f"On Linux, Cursor data is at ~/.config/Cursor/User/\n"
            f"On Windows, Cursor data is at %APPDATA%\\Cursor\\User\\",
            file=sys.stderr,
        )
        sys.exit(1)

    if not base.exists():
        print(
            f"Error: Cursor data directory not found at:\n"
            f"  {base}\n\n"
            f"This usually means:\n"
            f"  - Cursor is not installed on this machine, or\n"
            f"  - Cursor has never been opened (no data created yet), or\n"
            f"  - Cursor stores data at a non-standard location\n\n"
            f"Expected path for {system}: {base}",
            file=sys.stderr,
        )
        sys.exit(1)

    return base


def get_global_db_path() -> Path:
    """Return the path to Cursor's global state.vscdb."""
    return get_cursor_user_dir() / "globalStorage" / "state.vscdb"


def get_workspace_storage_dir() -> Path:
    """Return the path to Cursor's workspace storage directory."""
    return get_cursor_user_dir() / "workspaceStorage"


def get_cursor_projects_dir() -> Path:
    """Return the path to ~/.cursor/projects/ (agent transcripts, etc.)."""
    return Path.home() / ".cursor" / "projects"


def sanitize_project_path(project_path: str) -> str:
    """Convert a project path to Cursor's sanitized directory name format.

    /Users/callum/Desktop/Projects/myrepo -> Users-callum-Desktop-Projects-myrepo
    """
    # Strip leading slash/backslash and replace path separators with -
    return project_path.strip("/\\").replace("/", "-").replace("\\", "-")


def _decode_ssh_host(host: str) -> str:
    """Decode an SSH host identifier.

    Cursor encodes SSH hosts as hex-encoded JSON, e.g.:
    7b22686f73744e616d65223a22636f7265227d -> {"hostName":"core"} -> core
    """
    try:
        # Try to decode as hex
        decoded = bytes.fromhex(host).decode("utf-8")
        # Try to parse as JSON
        data = json.loads(decoded)
        if isinstance(data, dict) and "hostName" in data:
            return data["hostName"]
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return host


def _decode_file_uri(uri: str) -> str:
    """Decode a file:// URI to a local filesystem path.

    Handles URL-encoded characters (%20 -> space, %3A -> colon, etc.)
    and strips the leading slash on Windows drive paths (/C:/... -> C:/...).
    """
    path = unquote(uri[len("file://"):])
    # On Windows, file:///C:/... becomes /C:/... after stripping scheme
    if platform.system() == "Windows" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


def _parse_remote_uri(uri: str) -> tuple[Optional[str], str]:
    """Parse a vscode-remote:// URI into (host, filesystem_path).

    Format: vscode-remote://ssh-remote%2B<host>/<path>
            vscode-remote://wsl%2Bubuntu-24.04/<path>
    Returns (host, "") if the URI has no path portion.
    """
    host = None
    # Format: vscode-remote://ssh-remote%2B<host>/<path>
    authority = uri.split("/")[2]  # ssh-remote%2B<host> or wsl%2Bubuntu-...
    if "%2B" in authority:
        host = authority.split("%2B", 1)[1]
    elif "+" in authority:
        host = authority.split("+", 1)[1]
    # Decode the host if it's hex-encoded JSON (e.g. {"hostName":"core"})
    if host:
        host = unquote(host)
        host = _decode_ssh_host(host)
    parts = uri.split("/", 3)
    if len(parts) >= 4:
        return host, "/" + parts[3]
    return host, ""


def canonicalize_project_path(path: str) -> str:
    """Stable path key for matching the same repo across Cursor URI views.

    Examples (not machine-specific — any distro/host name):
      vscode-remote://wsl+Ubuntu-24.04/home/u/proj
          -> /home/u/proj
      file://wsl.localhost/Ubuntu-24.04/home/u/proj
          -> /home/u/proj
      file:////wsl.localhost/Ubuntu-24.04/home/u/proj
          -> /home/u/proj
      \\\\wsl$\\\\Ubuntu-24.04\\\\home\\\\u\\\\proj
          -> /home/u/proj
      \\home\\u\\proj  (Windows normpath of a remote path)
          -> /home/u/proj
      C:\\Users\\u\\proj
          -> c:/Users/u/proj  (Windows local; drive letter lowercased)

    Does not rewrite unrelated Windows paths into WSL form.
    """
    if not path:
        return ""

    raw = path.strip()
    if raw.startswith("file://"):
        raw = _decode_file_uri(raw)
    elif raw.startswith("vscode-remote://"):
        _host, remote_path = _parse_remote_uri(raw)
        raw = remote_path or raw

    # Unify separators; collapse duplicate slashes
    s = raw.replace("\\", "/")
    while "//" in s:
        s = s.replace("//", "/")

    # \\wsl.localhost\Distro\... or \\wsl$\Distro\... (and slash variants)
    m = re.match(
        r"^/*wsl(?:\.localhost|\$)/+([^/]+)/+(.*)$",
        s,
        flags=re.IGNORECASE,
    )
    if m:
        rest = m.group(2)
        if not rest.startswith("/"):
            rest = "/" + rest
        return (rest.rstrip("/") or "/")

    # Remote-style / POSIX path (including Windows-normalized "\home\...")
    if s.startswith("/") and not (len(s) > 2 and s[1].isalpha() and s[2] == ":"):
        return s.rstrip("/") or "/"

    # Local Windows drive path
    if platform.system() == "Windows":
        norm = os.path.normpath(raw).replace("\\", "/")
        if len(norm) >= 2 and norm[1] == ":":
            return (norm[0].lower() + norm[1:]).rstrip("/") or norm
        return norm.rstrip("/") or "/"

    return os.path.normpath(raw).replace("\\", "/").rstrip("/") or "/"


def _workspace_match_rank(ws: dict) -> tuple:
    """Sort key: prefer vscode-remote over wsl.localhost file://, then newer."""
    uri = (ws.get("folder_uri") or "").lower()
    if uri.startswith("vscode-remote://"):
        view_rank = 0
    elif "wsl.localhost" in uri or "wsl$" in uri:
        view_rank = 2
    else:
        view_rank = 1
    # Prefer ordinary folders over multi-root .code-workspace files
    type_rank = 1 if ws.get("type") == "workspace" else 0
    return (view_rank, type_rank, -float(ws.get("mtime") or 0))


def find_workspace_dirs_for_project(project_path: str) -> list[Path]:
    """Find all workspace directories that map to a given project path.

    Scans workspace.json files in workspaceStorage/ to find matches.
    Returns list of workspace directory paths, newest first.
    """
    ws_storage = get_workspace_storage_dir()
    if not ws_storage.exists():
        return []

    target = canonicalize_project_path(project_path)
    if platform.system() == "Windows":
        # Drive-letter paths are lowercased by canonicalize; remote POSIX paths
        # keep case. Also accept case-insensitive compare for local-only targets.
        target_cmp = target
    else:
        target_cmp = target

    matches = []
    for ws_dir in ws_storage.iterdir():
        if not ws_dir.is_dir():
            continue
        ws_json = ws_dir / "workspace.json"
        if not ws_json.exists():
            continue
        try:
            data = json.loads(ws_json.read_text())
            folder_uri = data.get("folder") or data.get("workspace") or ""
            if not folder_uri:
                continue
            if not (
                folder_uri.startswith("file://")
                or folder_uri.startswith("vscode-remote://")
            ):
                continue

            folder_canon = canonicalize_project_path(folder_uri)
            if folder_canon == target_cmp:
                matches.append(ws_dir)
        except (json.JSONDecodeError, OSError):
            continue

    # Sort by modification time, newest first
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches


def find_transcript_dir(project_path: str) -> Optional[Path]:
    """Find the agent-transcripts directory for a project."""
    projects_dir = get_cursor_projects_dir()
    if not projects_dir.exists():
        return None

    sanitized = sanitize_project_path(project_path)
    transcript_dir = projects_dir / sanitized / "agent-transcripts"
    if transcript_dir.exists():
        return transcript_dir

    return None


def get_project_path() -> str:
    """Get the current project path (current working directory)."""
    return os.getcwd()


def list_all_workspaces() -> list[dict]:
    """List all Cursor workspaces with metadata.

    Returns a list of dicts with:
      - folder_uri: raw URI from workspace.json
      - path: extracted filesystem path (for workspace, path to the .code-workspace file)
      - type: 'local', 'ssh', or 'workspace'
      - host: SSH hostname (for ssh type, None otherwise)
      - workspace_dir: Path to the workspace directory
      - mtime: modification time of the workspace DB
    """
    ws_storage = get_workspace_storage_dir()
    if not ws_storage.exists():
        return []

    workspaces = []
    for ws_dir in ws_storage.iterdir():
        if not ws_dir.is_dir():
            continue
        ws_json = ws_dir / "workspace.json"
        if not ws_json.exists():
            continue
        try:
            data = json.loads(ws_json.read_text())

            ws_type = "local"
            host = None
            folder_path = ""
            folder_uri = ""

            # workspace .code-workspace: uses "workspace" key instead of "folder"
            if "workspace" in data and not data.get("folder"):
                ws_uri = data["workspace"]
                folder_uri = ws_uri
                ws_type = "workspace"
                if ws_uri.startswith("file://"):
                    folder_path = _decode_file_uri(ws_uri)
                elif ws_uri.startswith("vscode-remote://"):
                    # Remote multi-root (.code-workspace via WSL/SSH), same
                    # authority/path parse as folder remotes — otherwise these
                    # workspaces are skipped and their chats look orphaned.
                    host, folder_path = _parse_remote_uri(ws_uri)
                    if not folder_path:
                        continue
                else:
                    continue
            else:
                folder_uri = data.get("folder", "")
                if not folder_uri:
                    continue

                if folder_uri.startswith("file://"):
                    folder_path = _decode_file_uri(folder_uri)
                elif folder_uri.startswith("vscode-remote://"):
                    ws_type = "ssh"
                    host, folder_path = _parse_remote_uri(folder_uri)
                    if not folder_path:
                        continue
                else:
                    continue

            # Get DB modification time
            db_path = ws_dir / "state.vscdb"
            mtime = db_path.stat().st_mtime if db_path.exists() else 0

            workspaces.append({
                "folder_uri": folder_uri,
                "path": os.path.normpath(folder_path),
                "canonical_path": canonicalize_project_path(folder_uri or folder_path),
                "type": ws_type,
                "host": host,
                "workspace_dir": ws_dir,
                "mtime": mtime,
            })
        except (json.JSONDecodeError, OSError):
            continue

    # Sort by modification time, newest first
    workspaces.sort(key=lambda w: w["mtime"], reverse=True)
    return workspaces


def get_global_composer_headers() -> list[dict]:
    """Return the global chat→workspace header index.

    Merges two sources used by different Cursor generations:

      1. Native SQL table ``composerHeaders`` (Cursor 3.x sidebar — current)
      2. ItemTable JSON ``composer.composerHeaders`` / ``allComposers``
         (3.0 migration index + cursaves dual-write)

    When the same ``composerId`` exists in both, the SQL row wins. Entries
    keep a ``workspaceIdentifier`` (id filled from the SQL ``workspaceId``
    column when needed) so callers can group by workspace hash.

    Returns an empty list if neither source is present.
    """
    from . import db

    global_db = get_global_db_path()
    if not global_db.exists():
        return []

    by_id: dict[str, dict] = {}
    try:
        with db.CursorDB(global_db) as cdb:
            # Legacy / dual-write JSON index first
            headers = cdb.get_json("composer.composerHeaders", table="ItemTable")
            if headers and isinstance(headers, dict):
                for entry in headers.get("allComposers", []):
                    if not isinstance(entry, dict):
                        continue
                    cid = entry.get("composerId")
                    if cid:
                        by_id[cid] = entry

            # Native SQL index overlays (authoritative for current Cursor)
            for entry in cdb.list_native_composer_headers():
                cid = entry.get("composerId")
                if cid:
                    by_id[cid] = entry
    except Exception:
        pass
    return list(by_id.values())


_global_headers_cache: Optional[dict[str, list[dict]]] = None


def _build_global_headers_map() -> dict[str, list[dict]]:
    """Build a workspace-hash → [composer header entries] map from the global index.

    Uses ``get_global_composer_headers()`` (SQL ∪ JSON merge). Keyed by
    ``workspaceIdentifier.id``. Cached for the lifetime of the process.
    """
    global _global_headers_cache
    if _global_headers_cache is not None:
        return _global_headers_cache

    result: dict[str, list[dict]] = {}
    for entry in get_global_composer_headers():
        wi = entry.get("workspaceIdentifier", {})
        if not isinstance(wi, dict):
            continue
        ws_id = wi.get("id", "")
        if ws_id:
            result.setdefault(ws_id, []).append(entry)
    _global_headers_cache = result
    return result


def invalidate_headers_cache():
    """Clear the cached global headers map (call after writing to the global DB)."""
    global _global_headers_cache
    _global_headers_cache = None


def get_workspace_composer_ids(ws_db_path: Path) -> list[str]:
    """Extract all composer IDs associated with a workspace.

    Combines multiple sources for maximum coverage:
    1. Global header index — native SQL ``composerHeaders`` ∪ ItemTable JSON
       (Cursor 3.x; SQL catches chats never written to the JSON blob)
    2. Workspace DB selectedComposerIds + composerChatViewPane entries
       (catches chats opened before the 3.0 migration that aren't yet
       in the global index)
    3. Workspace DB allComposers (Cursor 2.x fallback)

    Returns deduplicated IDs.
    """
    from . import db

    ids: set[str] = set()
    ws_hash = ws_db_path.parent.name

    # Source 1: global headers index (Cursor 3.0+)
    headers_map = _build_global_headers_map()
    for entry in headers_map.get(ws_hash, []):
        cid = entry.get("composerId")
        if cid:
            ids.add(cid)

    # Source 2+3: workspace DB
    try:
        with db.CursorDB(ws_db_path) as cdb:
            data = cdb.get_json("composer.composerData", table="ItemTable")
            if not data:
                return list(ids)  # headers-only workspace (Cursor 3.0+)

            # Cursor 2.x: allComposers (complete list for old workspaces)
            for c in data.get("allComposers", []):
                cid = c.get("composerId")
                if cid:
                    ids.add(cid)

            # Cursor 3.0+: supplementary sources for chats not in global index
            for cid in data.get("selectedComposerIds", []):
                if cid:
                    ids.add(cid)
            for cid in data.get("lastFocusedComposerIds", []):
                if cid:
                    ids.add(cid)

            for key in cdb.list_keys(
                "workbench.panel.composerChatViewPane.", table="ItemTable"
            ):
                pane = cdb.get_json(key, table="ItemTable")
                if isinstance(pane, dict):
                    for view_key in pane:
                        if ".view." in view_key:
                            cid = view_key.rsplit(".", 1)[-1]
                            if cid:
                                ids.add(cid)
    except Exception:
        pass

    return list(ids)


def list_workspaces_with_conversations() -> list[dict]:
    """List workspaces that have at least one conversation.

    Returns the same dicts as list_all_workspaces(), plus a
    'conversations' key with the count.
    """
    result = []
    for ws in list_all_workspaces():
        db_path = ws["workspace_dir"] / "state.vscdb"
        if not db_path.exists():
            continue
        composer_ids = get_workspace_composer_ids(db_path)
        if composer_ids:
            ws["conversations"] = len(composer_ids)
            result.append(ws)
    return result


def resolve_workspace(selector: str) -> Optional[dict]:
    """Resolve a workspace selector to a workspace dict.

    The selector can be:
      - A number (1-based index from list_workspaces_with_conversations)
      - A workspace hash (directory name under workspaceStorage/)
      - A path substring (matched against workspace paths)
    """
    workspaces = list_workspaces_with_conversations()

    # Try as index
    try:
        idx = int(selector)
        if workspaces and 1 <= idx <= len(workspaces):
            return workspaces[idx - 1]
        return None
    except ValueError:
        pass

    # Try as workspace hash (exact match, or prefix match when selector is 8 chars (short hash))
    # Allow the short hash because that's what's displayed in the workspaces list,
    # so user can just copy-paste the short hash, e.g. `cursaves push -w 497e8ab0`
    for ws in workspaces:
        name = ws["workspace_dir"].name
        if len(selector) == 8:
            # Short hash match (8 chars) - allow prefix match
            if name.startswith(selector):
                return ws
        else:
            # Exact match
            if name == selector:
                return ws

    # Try as path substring
    norm_selector = selector.replace("\\", "/")
    for ws in workspaces:
        norm_path = ws["path"].replace("\\", "/")
        if norm_selector in norm_path:
            return ws

    return None


def get_sync_dir() -> Path:
    """Return the cursaves sync directory (~/.cursaves/).

    This is the git repo that holds snapshots and is synced between machines.
    """
    return Path.home() / ".cursaves"


def get_snapshots_dir() -> Path:
    """Return the snapshots directory (~/.cursaves/snapshots/)."""
    snapshots = get_sync_dir() / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    return snapshots


def _get_config_dir() -> Path:
    """Return the cursaves config directory.

    Linux/macOS: ~/.config/cursaves/
    Windows: %APPDATA%/cursaves/
    """
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "cursaves"
    return Path.home() / ".config" / "cursaves"


def is_sync_repo_initialized() -> bool:
    """Check if a sync backend has been configured (git repo or cloud)."""
    sync_dir = get_sync_dir()
    if (sync_dir / ".git").exists():
        return True
    # Check for non-git backend config
    config_path = _get_config_dir() / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            return cfg.get("backend") in ("s3", "azure")
        except Exception:
            pass
    return False


def get_machine_id() -> str:
    """Return a human-readable machine identifier."""
    import socket

    return socket.gethostname()


# ── Workspace matching for imports ─────────────────────────────────────


def find_all_matching_workspaces(source_path: str) -> list[dict]:
    """Find all workspaces that could receive imports from source_path.

    Matches by:
    1. Canonical path equality (WSL remote / wsl.localhost / UNC / POSIX)
    2. Same basename (fallback for different directory structures)

    Within each tier, prefers vscode-remote:// targets over file://wsl.localhost,
    then ordinary folders over .code-workspace, then newer mtime.

    Returns list of workspace dicts with type, host, path, workspace_dir.
    """
    all_ws = list_all_workspaces()
    source_canon = canonicalize_project_path(source_path)
    source_basename = os.path.basename(
        source_canon or os.path.normpath(source_path)
    )

    exact_matches: list[dict] = []
    basename_matches: list[dict] = []
    seen: set[str] = set()

    for ws in all_ws:
        ws_dir_key = str(ws["workspace_dir"])
        ws_canon = ws.get("canonical_path") or canonicalize_project_path(
            ws.get("folder_uri") or ws["path"]
        )
        ws_basename = os.path.basename(ws_canon or ws["path"])

        if source_canon and ws_canon and source_canon == ws_canon:
            if ws_dir_key not in seen:
                exact_matches.append(ws)
                seen.add(ws_dir_key)
        elif source_basename and ws_basename == source_basename:
            if ws_dir_key not in seen:
                basename_matches.append(ws)
                seen.add(ws_dir_key)

    exact_matches.sort(key=_workspace_match_rank)
    basename_matches.sort(key=_workspace_match_rank)

    # Same repo under WSL remote + wsl.localhost + UNC shares one canonical
    # path — keep the best-ranked view so pull doesn't prompt on equivalents.
    return _collapse_equivalent_workspaces(exact_matches + basename_matches)


def _collapse_equivalent_workspaces(workspaces: list[dict]) -> list[dict]:
    """Dedupe workspaces that canonicalize to the same project path."""
    best: dict[str, dict] = {}
    order: list[str] = []
    passthrough: list[dict] = []
    for ws in workspaces:
        key = ws.get("canonical_path") or canonicalize_project_path(
            ws.get("folder_uri") or ws["path"]
        )
        if not key:
            passthrough.append(ws)
            continue
        if key not in best:
            best[key] = ws
            order.append(key)
        elif _workspace_match_rank(ws) < _workspace_match_rank(best[key]):
            best[key] = ws
    return [best[k] for k in order] + passthrough


def format_workspace_display(ws: dict, include_path: bool = True) -> str:
    """Format a workspace dict for display.

    Returns a string like "ssh core /mnt/home/.../project", "(local) /home/.../project",
    or "(workspace) /home/.../my-proj.code-workspace"
    """
    if ws["type"] == "ssh":
        host = ws.get("host") or "unknown"
        if include_path:
            path = ws["path"]
            if len(path) > 40:
                path = "..." + path[-37:]
            return f"ssh {host} {path}"
        return f"ssh {host}"
    elif ws["type"] == "workspace":
        host = ws.get("host")
        if include_path:
            path = ws["path"]
            if len(path) > 45:
                path = "..." + path[-42:]
            if host:
                return f"(workspace) {host} {path}"
            return f"(workspace) {path}"
        return f"(workspace) {host}" if host else "(workspace)"
    else:
        if include_path:
            path = ws["path"]
            if len(path) > 45:
                path = "..." + path[-42:]
            return f"(local) {path}"
        return "(local)"


# ── Project identification ────────────────────────────────────────────


_git_remote_cache: dict[str, Optional[str]] = {}
_project_id_cache: dict[str, str] = {}


def get_project_identifier(project_path: str) -> str:
    """Get a stable identifier for a project, used as the snapshot subdirectory.

    Uses the git remote origin URL if available (normalized to a filesystem-safe
    string).  Falls back to the directory basename for non-git projects.

    This means:
      - Same repo under different local names (bob/ vs alice/) → same identifier
      - Different repos that happen to share a name → different identifiers
    """
    normalized_path = os.path.normpath(project_path)
    if normalized_path in _project_id_cache:
        return _project_id_cache[normalized_path]

    remote_url = _get_git_remote_url(project_path)
    if remote_url:
        ident = _normalize_remote_url(remote_url)
    else:
        ident = os.path.basename(normalized_path)

    _project_id_cache[normalized_path] = ident
    return ident


def _get_git_remote_url(project_path: str) -> Optional[str]:
    """Get the git remote origin URL for a project, if any."""
    normalized_path = os.path.normpath(project_path)
    if normalized_path in _git_remote_cache:
        return _git_remote_cache[normalized_path]

    url = None
    try:
        result = subprocess.run(
            ["git", "-C", project_path, "config", "--get", "remote.origin.url"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            url = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    _git_remote_cache[normalized_path] = url
    return url


def _normalize_remote_url(url: str) -> str:
    """Normalize a git remote URL to a stable, filesystem-safe directory name.

    git@github.com:user/repo.git     → github.com-user-repo
    https://github.com/user/repo.git → github.com-user-repo
    ssh://git@github.com/user/repo   → github.com-user-repo
    """
    # Strip trailing .git
    url = re.sub(r"\.git$", "", url)

    # SSH shorthand: git@host:user/repo
    m = re.match(r"^[\w.-]+@([\w.-]+):(.*)", url)
    if m:
        host, path = m.group(1), m.group(2)
        return _sanitize_identifier(f"{host}/{path}")

    # HTTPS / SSH URI: https://host/path or ssh://git@host/path
    m = re.match(r"^(?:https?|ssh)://(?:[\w.-]+@)?([\w.-]+)/(.*)", url)
    if m:
        host, path = m.group(1), m.group(2)
        return _sanitize_identifier(f"{host}/{path}")

    # Unknown format -- sanitize whatever we got
    return _sanitize_identifier(url)


def _sanitize_identifier(s: str) -> str:
    """Turn an arbitrary string into a safe directory name.

    Replaces slashes, colons, @, etc. with '-' and collapses runs of dashes.
    """
    s = re.sub(r"[/:@\\]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")
