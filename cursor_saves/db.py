"""Safe SQLite reader/writer for Cursor's state.vscdb databases."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Optional


class CursorDB:
    """Safe interface to a Cursor state.vscdb database.

    Reads use a direct SQLite URI when possible (no multi-GB copy). If that
    fails, they fall back to a temporary copy so a running Cursor instance
    cannot lock us out. Writes operate on the original file and require
    Cursor to be closed.
    """

    def __init__(self, db_path: Path, no_copy: bool = True):
        self.db_path = db_path
        self.no_copy = no_copy
        self._tmp_path: Optional[Path] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._in_transaction = False

    @staticmethod
    def _probe_readable(conn: sqlite3.Connection) -> None:
        """Raise OperationalError if the connection cannot read Cursor tables."""
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('cursorDiskKV', 'ItemTable')"
            ).fetchall()
        ]
        if not tables:
            raise sqlite3.OperationalError("no Cursor tables visible")
        for table in tables:
            conn.execute(f"SELECT key FROM {table} LIMIT 1").fetchone()

    def _try_uri_readonly(self, query: str) -> Optional[sqlite3.Connection]:
        """Open a read-only URI connection and probe Cursor tables. None on failure."""
        conn: Optional[sqlite3.Connection] = None
        try:
            db_uri = f"{self.db_path.resolve().as_uri()}?{query}"
            conn = sqlite3.connect(db_uri, uri=True, timeout=30)
            conn.execute("SELECT 1").fetchone()
            self._probe_readable(conn)
            return conn
        except sqlite3.OperationalError:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            return None

    def _ensure_read_copy(self) -> sqlite3.Connection:
        """Open a read connection, copying the DB only as a last resort.

        A full copy of Cursor's global DB is often multi-GB and is the usual
        reason a tiny import appears to hang for minutes.
        """
        if self._conn is not None:
            return self._conn

        if not self.db_path.exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")

        # 1) nolock URI — fastest, works while Cursor is running.
        # 2) mode=ro without nolock — proper WAL view; needed when nolock
        #    returns empty tables (macOS paths with spaces, active writer).
        # 3) Copy WAL+main to temp — last resort only.
        if self.no_copy:
            for query in ("mode=ro&nolock=1", "mode=ro"):
                conn = self._try_uri_readonly(query)
                if conn is not None:
                    self._conn = conn
                    return self._conn

        size_mb = _file_mb(self.db_path)
        if size_mb >= 50:
            print(
                f"  Note: copying {size_mb:.0f} MB database for a consistent read "
                f"(direct SQLite open failed). This can take a while.",
                file=sys.stderr,
                flush=True,
            )

        tmp_dir = tempfile.mkdtemp(prefix="cursaves-")
        tmp_db = Path(tmp_dir) / "state.vscdb"
        _copy_file_fast(self.db_path, tmp_db)

        for suffix in ("-wal", "-shm"):
            wal_file = self.db_path.parent / (self.db_path.name + suffix)
            if wal_file.exists():
                _copy_file_fast(wal_file, Path(tmp_dir) / (tmp_db.name + suffix))

        self._tmp_path = tmp_db
        self._conn = sqlite3.connect(str(tmp_db))
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        return self._conn

    def _query_conn(self) -> sqlite3.Connection:
        """Connection for reads: reuse an open write conn, else a read conn.

        Import opens a write connection on the original file. Reading the
        giant composer.composerHeaders blob through that connection avoids
        a second (sometimes copying) handle on a multi-GB DB.
        """
        write_conn = getattr(self, "_write_conn", None)
        if write_conn is not None:
            return write_conn
        return self._ensure_read_copy()

    def close(self):
        """Close connections and clean up temp files."""
        if self._conn:
            self._conn.close()
            self._conn = None
        if hasattr(self, "_write_conn") and self._write_conn:
            self._write_conn.close()
            self._write_conn = None
        if self._tmp_path:
            shutil.rmtree(self._tmp_path.parent, ignore_errors=True)
            self._tmp_path = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self):
        """Context manager to run multiple write operations in a single transaction."""
        conn = self._get_write_conn()
        if self._in_transaction:
            yield
            return

        self._in_transaction = True
        conn.execute("BEGIN")
        try:
            yield
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            self._in_transaction = False

    # ── Read operations ─────────────────────────────────────────────

    def get_item(self, key: str, table: str = "ItemTable") -> Optional[str]:
        """Get a value from the key-value store."""
        conn = self._query_conn()
        try:
            row = conn.execute(
                f"SELECT value FROM {table} WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            val = row[0]
            if isinstance(val, bytes):
                return val.decode("utf-8", errors="replace")
            return val
        except sqlite3.OperationalError:
            return None

    def get_item_binary(self, key: str, table: str = "ItemTable") -> Optional[bytes]:
        """Get a raw binary value from the key-value store."""
        conn = self._query_conn()
        try:
            row = conn.execute(
                f"SELECT value FROM {table} WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            val = row[0]
            if isinstance(val, str):
                return val.encode("utf-8")
            return val
        except sqlite3.OperationalError:
            return None

    def get_disk_kv(self, key: str) -> Optional[str]:
        """Get a value from the cursorDiskKV table."""
        return self.get_item(key, table="cursorDiskKV")

    def list_keys(self, prefix: str = "", table: str = "cursorDiskKV") -> list[str]:
        """List all keys in a table, optionally filtered by prefix."""
        conn = self._query_conn()
        try:
            if prefix:
                rows = conn.execute(
                    f"SELECT key FROM {table} WHERE key LIKE ?", (prefix + "%",)
                ).fetchall()
            else:
                rows = conn.execute(f"SELECT key FROM {table}").fetchall()
            return [r[0] for r in rows]
        except sqlite3.OperationalError:
            return []

    def count_keys_by_chat_prefix(
        self, key_type: str, table: str = "cursorDiskKV"
    ) -> dict[str, int]:
        """Count keys grouped by chat ID for a given key type prefix.

        For example, key_type="bubbleId" counts all keys like
        "bubbleId:<uuid>:..." and returns {<uuid>: count, ...}.

        Uses a single SQL query — efficient even on large databases.
        """
        conn = self._query_conn()
        result: dict[str, int] = {}
        try:
            prefix = key_type + ":"
            rows = conn.execute(
                f"""SELECT SUBSTR(key, {len(prefix) + 1}, 36) AS cid, COUNT(*)
                    FROM {table}
                    WHERE key LIKE ?
                    GROUP BY cid""",
                (prefix + "%",),
            ).fetchall()
            for cid, count in rows:
                if cid and len(cid) == 36:
                    result[cid] = count
        except sqlite3.OperationalError:
            pass
        return result

    def get_json(self, key: str, table: str = "cursorDiskKV") -> Optional[Any]:
        """Get and parse a JSON value."""
        raw = self.get_item(key, table=table)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def get_items_by_prefix(self, prefix: str, table: str = "cursorDiskKV") -> dict[str, str]:
        """Get all key-value pairs matching a prefix as strings."""
        conn = self._query_conn()
        try:
            rows = conn.execute(
                f"SELECT key, value FROM {table} WHERE key LIKE ?", (prefix + "%",)
            ).fetchall()
            result = {}
            for k, v in rows:
                if isinstance(v, bytes):
                    result[k] = v.decode("utf-8", errors="replace")
                elif v is not None:
                    result[k] = v
            return result
        except sqlite3.OperationalError:
            return {}

    def get_json_items_by_prefix(self, prefix: str, table: str = "cursorDiskKV") -> dict[str, Any]:
        """Get all key-value pairs matching a prefix and parse values as JSON."""
        conn = self._query_conn()
        try:
            rows = conn.execute(
                f"SELECT key, value FROM {table} WHERE key LIKE ?", (prefix + "%",)
            ).fetchall()
            result = {}
            for k, v in rows:
                if isinstance(v, bytes):
                    v = v.decode("utf-8", errors="replace")
                try:
                    result[k] = json.loads(v) if v is not None else None
                except json.JSONDecodeError:
                    result[k] = None
            return result
        except sqlite3.OperationalError:
            return {}

    def list_native_composer_headers(self) -> list[dict]:
        """Read Cursor 3.x native ``composerHeaders`` SQL table as header dicts.

        Each row's ``value`` JSON is returned, with ``workspaceIdentifier.id``
        taken from the ``workspaceId`` column (sidebar index key). Returns []
        if the table is absent (older Cursor builds).
        """
        conn = self._query_conn()
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='composerHeaders'"
            ).fetchone()
            if not exists:
                return []
            rows = conn.execute(
                "SELECT composerId, workspaceId, value FROM composerHeaders"
            ).fetchall()
        except sqlite3.OperationalError:
            return []

        out: list[dict] = []
        for cid, workspace_id, value in rows:
            entry: Optional[dict] = None
            if value is not None:
                try:
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", errors="replace")
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        entry = parsed
                except (json.JSONDecodeError, TypeError):
                    entry = None
            if entry is None:
                entry = {}
            if cid and not entry.get("composerId"):
                entry["composerId"] = cid
            if workspace_id:
                wi = entry.get("workspaceIdentifier")
                if not isinstance(wi, dict):
                    wi = {}
                else:
                    wi = dict(wi)
                wi["id"] = workspace_id
                entry["workspaceIdentifier"] = wi
            if entry.get("composerId"):
                out.append(entry)
        return out

    def get_items_by_keys_binary(self, keys: list[str], table: str = "cursorDiskKV") -> dict[str, bytes]:
        """Get multiple raw binary values from the key-value store in a single query."""
        if not keys:
            return {}
        conn = self._query_conn()
        result = {}
        try:
            for i in range(0, len(keys), 500):
                batch = keys[i : i + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT key, value FROM {table} WHERE key IN ({placeholders})", batch
                ).fetchall()
                for k, v in rows:
                    if isinstance(v, str):
                        result[k] = v.encode("utf-8")
                    elif v is not None:
                        result[k] = v
            return result
        except sqlite3.OperationalError:
            return {}

    def get_items_by_keys(self, keys: list[str], table: str = "cursorDiskKV") -> dict[str, str]:
        """Get multiple string values by exact keys (batched IN queries)."""
        raw = self.get_items_by_keys_binary(keys, table=table)
        out: dict[str, str] = {}
        for k, v in raw.items():
            if isinstance(v, bytes):
                out[k] = v.decode("utf-8", errors="replace")
            elif v is not None:
                out[k] = str(v)
        return out

    # ── Write operations (on original file) ─────────────────────────

    def _get_write_conn(self) -> sqlite3.Connection:
        """Get or create a connection for write operations on the ORIGINAL database."""
        if not hasattr(self, "_write_conn") or self._write_conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=60)
            # Per-connection (not persisted). NORMAL is safe with WAL and avoids
            # an fsync on every commit against a multi-GB global DB.
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            self._write_conn = conn
        return self._write_conn

    def write_item(self, key: str, value: str, table: str = "ItemTable"):
        """Write a value to the key-value store on the ORIGINAL database.

        This operates directly on the original file, not the temp copy.
        Caller must ensure Cursor is not running.
        """
        conn = self._get_write_conn()
        conn.execute(
            f"INSERT OR REPLACE INTO {table} (key, value) VALUES (?, ?)",
            (key, value),
        )
        if not self._in_transaction:
            conn.commit()

    def write_disk_kv(self, key: str, value: str):
        """Write a value to cursorDiskKV on the ORIGINAL database."""
        self.write_item(key, value, table="cursorDiskKV")

    def write_json(self, key: str, data: Any, table: str = "cursorDiskKV"):
        """Write a JSON value to the ORIGINAL database."""
        self.write_item(key, json.dumps(data, separators=(",", ":")), table=table)

    def write_batch(self, items: list[tuple[str, str]], table: str = "cursorDiskKV"):
        """Write multiple key-value pairs in a single transaction.

        Much faster than calling write_item() in a loop -- uses one
        connection and one transaction for all items.
        """
        conn = self._get_write_conn()
        in_txn = self._in_transaction
        if not in_txn:
            conn.execute("BEGIN")
        try:
            conn.executemany(
                f"INSERT OR REPLACE INTO {table} (key, value) VALUES (?, ?)",
                items,
            )
            if not in_txn:
                conn.execute("COMMIT")
        except Exception:
            if not in_txn:
                conn.execute("ROLLBACK")
            raise

    def write_json_batch(self, items: list[tuple[str, Any]], table: str = "cursorDiskKV"):
        """Write multiple JSON key-value pairs in a single transaction."""
        serialized = [
            (key, json.dumps(data, separators=(",", ":")))
            for key, data in items
        ]
        self.write_batch(serialized, table=table)

    def delete_keys(self, keys: list[str], table: str = "cursorDiskKV") -> int:
        """Delete multiple keys in a single transaction on the ORIGINAL database.

        Returns the number of rows deleted.
        """
        if not keys:
            return 0
        conn = self._get_write_conn()
        in_txn = self._in_transaction
        if not in_txn:
            conn.execute("BEGIN")
        try:
            total = 0
            for batch_start in range(0, len(keys), 500):
                batch = keys[batch_start:batch_start + 500]
                placeholders = ",".join("?" for _ in batch)
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE key IN ({placeholders})", batch
                )
                total += cur.rowcount
            if not in_txn:
                conn.execute("COMMIT")
            return total
        except Exception:
            if not in_txn:
                conn.execute("ROLLBACK")
            raise

    def delete_keys_by_prefix(self, prefix: str, table: str = "cursorDiskKV") -> int:
        """Delete all keys matching a prefix on the ORIGINAL database.

        Returns the number of rows deleted.
        """
        conn = self._get_write_conn()
        cur = conn.execute(
            f"DELETE FROM {table} WHERE key LIKE ?", (prefix + "%",)
        )
        if not self._in_transaction:
            conn.commit()
        return cur.rowcount



def _file_mb(path: Path) -> float:
    try:
        return path.stat().st_size / (1024 * 1024)
    except OSError:
        return 0.0


_COPY_CHUNK = 8 * 1024 * 1024  # 8 MiB — small enough that Ctrl+C is noticed


def _copy_file_bytes(src: Path, dst: Path) -> None:
    """Byte-copy *src* to *dst* in Python chunks so KeyboardInterrupt works.

    shutil.copy2 uses sendfile/fcopyfile — a single uninterruptible kernel
    call — which is why Ctrl+C appears to do nothing during a multi-GB backup.
    """
    total = src.stat().st_size
    copied = 0
    last_report = time.monotonic()
    if dst.exists():
        dst.unlink()
    try:
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            while True:
                buf = fsrc.read(_COPY_CHUNK)
                if not buf:
                    break
                fdst.write(buf)
                copied += len(buf)
                now = time.monotonic()
                if total >= 50 * 1024 * 1024 and now - last_report >= 2.0:
                    print(
                        f"    {copied / (1024 * 1024):.0f} / {total / (1024 * 1024):.0f} MB "
                        f"(Ctrl+C to abort)",
                        flush=True,
                    )
                    last_report = now
        shutil.copystat(src, dst, follow_symlinks=True)
    except BaseException:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _copy_file_fast(src: Path, dst: Path) -> str:
    """Copy *src* to *dst*, using a copy-on-write clone when the FS supports it.

    Returns ``"clone"`` or ``"copy"``. CoW clones (APFS clonefile, Linux FICLONE)
    make backing up a multi-GB Cursor DB effectively instant; a byte copy of
    that same file is often the multi-minute wait behind a tiny chat import.
    """
    import platform

    system = platform.system()
    if dst.exists():
        dst.unlink()

    if system == "Darwin":
        try:
            import ctypes

            libc = ctypes.CDLL("/usr/lib/libc.dylib", use_errno=True)
            clonefile = libc.clonefile
            clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
            clonefile.restype = ctypes.c_int
            if clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0:
                shutil.copystat(src, dst, follow_symlinks=True)
                return "clone"
        except Exception:
            if dst.exists():
                try:
                    dst.unlink()
                except OSError:
                    pass
    elif system == "Linux":
        # FICLONE: _IOW(0x94, 9, int) on 64-bit Linux
        FICLONE = 0x40049409
        try:
            import fcntl

            with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                fcntl.ioctl(fdst.fileno(), FICLONE, fsrc.fileno())
            shutil.copystat(src, dst, follow_symlinks=True)
            return "clone"
        except OSError:
            if dst.exists():
                try:
                    dst.unlink()
                except OSError:
                    pass

    _copy_file_bytes(src, dst)
    return "copy"


def backup_db(db_path: Path, keep: int = 2) -> Path:
    """Create a timestamped backup of a database file.

    Keeps only the most recent `keep` backups (default 2) and deletes
    older ones to prevent unbounded disk usage. The global DB can be
    multi-GB, so even a handful of stale backups can fill a disk.

    Uses a copy-on-write clone when the filesystem supports it (APFS,
    btrfs, XFS reflink) so a multi-GB backup is not a multi-minute copy.

    Returns the path to the new backup.
    """
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = db_path.parent / f"{db_path.stem}.backup_{timestamp}{db_path.suffix}"

    size_mb = _file_mb(db_path)
    wal = db_path.parent / (db_path.name + "-wal")
    wal_mb = _file_mb(wal) if wal.exists() else 0.0
    extra = f" + {wal_mb:.0f} MB WAL" if wal_mb >= 1 else ""
    print(f"  Backing up {db_path.name} ({size_mb:.0f} MB{extra})...", flush=True)
    t0 = time.monotonic()

    try:
        method = _copy_file_fast(db_path, backup_path)
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.parent / (db_path.name + suffix)
            if sidecar.exists():
                _copy_file_fast(sidecar, db_path.parent / (backup_path.name + suffix))
    except KeyboardInterrupt:
        print("  Backup aborted (original DB untouched).", file=sys.stderr, flush=True)
        for leftover in (backup_path, *(
            db_path.parent / (backup_path.name + s) for s in ("-wal", "-shm")
        )):
            leftover.unlink(missing_ok=True)
        raise

    elapsed = time.monotonic() - t0
    how = "Cloned" if method == "clone" else "Copied"
    print(f"  {how} in {elapsed:.1f}s → {backup_path.name}", flush=True)

    # Clean up old backups, keeping only the newest `keep`.
    # Sort by filename (embedded timestamp), not mtime: copy2/clonefile
    # preserves the source mtime so all backups can share one timestamp.
    pattern = f"{db_path.stem}.backup_*{db_path.suffix}"
    old_backups = sorted(
        db_path.parent.glob(pattern),
        reverse=True,
    )
    for stale in old_backups[keep:]:
        stale.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            sidecar = stale.parent / (stale.name + suffix)
            sidecar.unlink(missing_ok=True)

    return backup_path
