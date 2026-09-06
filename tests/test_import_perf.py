"""Tests for faster single-chat import (v0.9.16).

Import time was dominated by multi-GB copies of Cursor's global DB, not
by the 20 messages in the snapshot.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_saves import db
from cursor_saves import importer as importer_mod


def _init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE, value BLOB)")
    conn.execute("CREATE TABLE IF NOT EXISTS cursorDiskKV (key TEXT UNIQUE, value BLOB)")
    conn.commit()
    conn.close()


class TestFastFileCopy(unittest.TestCase):
    def test_copy_file_bytes_is_interruptible_and_cleans_dst(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.bin"
            dst = Path(tmp) / "dst.bin"
            src.write_bytes(b"x" * (db._COPY_CHUNK * 4))
            reads = {"n": 0}
            real_open = open

            def wrapping_open(path, mode="r", *args, **kwargs):
                fh = real_open(path, mode, *args, **kwargs)
                if "r" in mode and Path(path) == src:
                    inner_read = fh.read

                    def counting_read(n=-1):
                        reads["n"] += 1
                        if reads["n"] >= 2:
                            raise KeyboardInterrupt
                        return inner_read(n)

                    fh.read = counting_read  # type: ignore[method-assign]
                return fh

            with patch("cursor_saves.db.open", wrapping_open):
                with self.assertRaises(KeyboardInterrupt):
                    db._copy_file_bytes(src, dst)
            self.assertFalse(dst.exists())

    def test_copy_file_fast_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.bin"
            dst = Path(tmp) / "dst.bin"
            payload = b"hello-cursaves" * 1000
            src.write_bytes(payload)
            method = db._copy_file_fast(src, dst)
            self.assertIn(method, ("clone", "copy"))
            self.assertEqual(dst.read_bytes(), payload)

    def test_backup_db_creates_file_and_keeps_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "state.vscdb"
            src.write_bytes(b"db-bytes")
            p1 = db.backup_db(src, keep=2)
            self.assertTrue(p1.exists())
            self.assertEqual(p1.read_bytes(), b"db-bytes")
            p2 = db.backup_db(src, keep=1)
            self.assertTrue(p2.exists())
            self.assertFalse(p1.exists())


class TestDirectDbOpen(unittest.TestCase):
    def test_read_does_not_copy_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.vscdb"
            _init_db(path)
            with db.CursorDB(path) as cdb:
                cdb.write_json("composerData:x", {"name": "n"})
            with db.CursorDB(path) as cdb:
                data = cdb.get_json("composerData:x")
                self.assertEqual(data["name"], "n")
                self.assertIsNone(cdb._tmp_path)

    def test_read_db_under_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Application Support" / "Cursor" / "state.vscdb"
            _init_db(path)
            with db.CursorDB(path) as cdb:
                cdb.write_json("k", {"ok": True}, table="ItemTable")
            with db.CursorDB(path) as cdb:
                self.assertEqual(cdb.get_json("k", table="ItemTable"), {"ok": True})
                self.assertIsNone(cdb._tmp_path)

    def test_reads_reuse_write_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.vscdb"
            _init_db(path)
            cdb = db.CursorDB(path)
            try:
                cdb.write_json("composerData:x", {"a": 1})
                self.assertIs(cdb._query_conn(), cdb._write_conn)
            finally:
                cdb.close()

    def test_write_conn_sets_synchronous_normal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.vscdb"
            _init_db(path)
            with db.CursorDB(path) as cdb:
                conn = cdb._get_write_conn()
                mode = conn.execute("PRAGMA synchronous").fetchone()[0]
                # 1 = NORMAL, 2 = FULL
                self.assertEqual(mode, 1)


class TestRegisterHeadersReuse(unittest.TestCase):
    def test_register_reuses_open_global_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws_dir = root / "ws"
            ws_dir.mkdir()
            gdb = root / "global.vscdb"
            _init_db(gdb)
            ws_json = ws_dir / "workspace.json"
            ws_json.write_text(json.dumps({"folder": "file:///tmp/proj"}))

            cdb = db.CursorDB(gdb)
            try:
                with patch.object(importer_mod.paths, "get_global_db_path", return_value=gdb):
                    importer_mod._register_in_global_headers(
                        "cid-1",
                        {"name": "tiny", "createdAt": 1, "lastUpdatedAt": 2},
                        ws_dir,
                        global_cdb=cdb,
                    )
                headers = cdb.get_json("composer.composerHeaders", table="ItemTable")
                ids = {e["composerId"] for e in headers["allComposers"]}
                self.assertIn("cid-1", ids)
            finally:
                cdb.close()

            # Must not have left a second write handle that created junk
            self.assertTrue(gdb.exists())


if __name__ == "__main__":
    unittest.main()
