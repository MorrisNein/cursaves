"""Tests for content/agent blob heal on import, repair, and pull gating (v0.9.14)."""

from __future__ import annotations

import base64
import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_saves import db, importer as importer_mod
from cursor_saves.cli import _pull_behind


CONTENT_HASH = "a" * 64
BUBBLE_ID = "11111111-1111-1111-1111-111111111111"
COMPOSER_ID = "22222222-2222-2222-2222-222222222222"


def _init_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE, value BLOB)")
    conn.execute("CREATE TABLE IF NOT EXISTS cursorDiskKV (key TEXT UNIQUE, value BLOB)")
    conn.commit()
    conn.close()


def _composer_data(name: str = "heal-chat", content_hash: str = CONTENT_HASH) -> dict:
    return {
        "name": name,
        "fullConversationHeadersOnly": [{"bubbleId": BUBBLE_ID}],
        "context": {"fileHash": content_hash},
    }


def _write_snapshot(path: Path, content_blobs: dict | None = None, agent_blobs: dict | None = None) -> None:
    snap = {
        "version": 3,
        "composerId": COMPOSER_ID,
        "exportedAt": "2026-01-01T00:00:00+00:00",
        "sourceMachine": "test",
        "sourceProjectPath": "/tmp/proj",
        "projectIdentifier": "proj",
        "composerData": _composer_data(),
        "contentBlobs": content_blobs or {},
        "bubbleEntries": {BUBBLE_ID: {"type": 1, "text": "hi"}},
        "agentBlobs": agent_blobs or {},
        "messageContexts": {},
        "checkpoints": {},
        "plans": [],
    }
    path.write_bytes(gzip.compress(json.dumps(snap).encode("utf-8")))


class TestHealMissingSnapshotBlobs(unittest.TestCase):
    def test_identical_import_fills_missing_content_blob(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gdb = root / "state.vscdb"
            _init_db(gdb)
            with db.CursorDB(gdb) as cdb:
                cdb.write_json(f"composerData:{COMPOSER_ID}", _composer_data())
                cdb.write_json(
                    f"bubbleId:{COMPOSER_ID}:{BUBBLE_ID}",
                    {"type": 1, "text": "hi"},
                )
                # Pre-existing blob that must not be overwritten
                other = "b" * 64
                cdb.write_batch([(f"composer.content.{other}", "KEEP")])

            snap = root / f"{COMPOSER_ID}.json.gz"
            _write_snapshot(
                snap,
                content_blobs={CONTENT_HASH: "RESTORED", other: "SHOULD_NOT_WRITE"},
            )

            with (
                patch.object(importer_mod.paths, "get_global_db_path", return_value=gdb),
                patch.object(importer_mod, "_maybe_restore_plans"),
            ):
                ok = importer_mod.import_snapshot(snap, "/tmp/proj", skip_backup=True)

            self.assertTrue(ok)
            with db.CursorDB(gdb) as cdb:
                self.assertEqual(
                    cdb.get_item(f"composer.content.{CONTENT_HASH}", table="cursorDiskKV"),
                    "RESTORED",
                )
                self.assertEqual(
                    cdb.get_item(f"composer.content.{other}", table="cursorDiskKV"),
                    "KEEP",
                )

    def test_heal_helper_fill_only_and_agent_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            gdb = Path(tmp) / "state.vscdb"
            _init_db(gdb)
            agent_id = "c" * 64
            payload = b"\x00agent-bytes"
            with db.CursorDB(gdb) as cdb:
                cdb.write_batch([(f"composer.content.{CONTENT_HASH}", "EXISTING")])
                cn, an = importer_mod._heal_missing_snapshot_blobs(
                    cdb,
                    {CONTENT_HASH: "NEW", "d" * 64: "FILLED"},
                    {agent_id: base64.b64encode(payload).decode("ascii")},
                )
            self.assertEqual(cn, 1)
            self.assertEqual(an, 1)
            with db.CursorDB(gdb) as cdb:
                self.assertEqual(
                    cdb.get_item(f"composer.content.{CONTENT_HASH}", table="cursorDiskKV"),
                    "EXISTING",
                )
                self.assertEqual(
                    cdb.get_item(f"composer.content.{'d' * 64}", table="cursorDiskKV"),
                    "FILLED",
                )
                self.assertEqual(
                    cdb.get_item_binary(f"agentKv:blob:{agent_id}", table="cursorDiskKV"),
                    payload,
                )


class TestRepairMissingContentBlobs(unittest.TestCase):
    def test_repair_restores_content_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gdb = root / "state.vscdb"
            snaps = root / "snapshots" / "proj"
            snaps.mkdir(parents=True)
            _init_db(gdb)
            with db.CursorDB(gdb) as cdb:
                cdb.write_json(f"composerData:{COMPOSER_ID}", _composer_data())
                cdb.write_json(
                    f"bubbleId:{COMPOSER_ID}:{BUBBLE_ID}",
                    {"type": 1, "text": "hi", "attachment": CONTENT_HASH},
                )

            _write_snapshot(snaps / f"{COMPOSER_ID}.json.gz", content_blobs={CONTENT_HASH: "FROM_SNAP"})
            (snaps / f"{COMPOSER_ID}.meta.json").write_text(
                json.dumps(
                    {
                        "composerId": COMPOSER_ID,
                        "messageCount": 1,
                        "version": 3,
                        "contentBlobCount": 1,
                        "agentBlobCount": None,
                    }
                )
            )

            with (
                patch.object(importer_mod.paths, "get_global_db_path", return_value=gdb),
                patch.object(importer_mod.paths, "get_snapshots_dir", return_value=root / "snapshots"),
                patch.object(importer_mod.db, "backup_db", return_value=Path("backup.vscdb")),
            ):
                fixed, restored = importer_mod.repair_missing_blobs(verbose=False)

            self.assertEqual(fixed, 1)
            self.assertEqual(restored, 1)
            with db.CursorDB(gdb) as cdb:
                self.assertEqual(
                    cdb.get_item(f"composer.content.{CONTENT_HASH}", table="cursorDiskKV"),
                    "FROM_SNAP",
                )


class TestPullBehindBlobGating(unittest.TestCase):
    def _run_queue_collect(self, meta: dict, missing: bool):
        """Return whether import_snapshot was called for the meta under full scan."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gdb = root / "state.vscdb"
            _init_db(gdb)
            proj = root / "snapshots" / "proj"
            proj.mkdir(parents=True)
            sf = proj / f"{COMPOSER_ID}.json.gz"
            sf.write_bytes(b"unused")
            (proj / f"{COMPOSER_ID}.meta.json").write_text(json.dumps(meta))

            imported: list[str] = []

            def fake_import(path, *args, **kwargs):
                imported.append(str(path))
                return True

            ws = {
                "workspace_dir": root / "ws",
                "path": "/tmp/proj",
            }
            (root / "ws").mkdir()
            _init_db(root / "ws" / "state.vscdb")

            with (
                patch("cursor_saves.cli.list_snapshot_projects", return_value=[{
                    "name": "proj",
                    "path": proj,
                    "count": 1,
                    "source_paths": {"/tmp/proj"},
                    "sources": set(),
                }]),
                patch("cursor_saves.cli.list_snapshot_files", return_value=[sf]),
                patch("cursor_saves.cli.read_snapshot_meta", return_value=meta),
                patch(
                    "cursor_saves.cli.get_sync_status_for_snapshot",
                    return_value="up_to_date",
                ),
                patch(
                    "cursor_saves.cli._local_composer_missing_blobs",
                    return_value=missing,
                ),
                patch("cursor_saves.cli.import_snapshot", side_effect=fake_import),
                patch(
                    "cursor_saves.cli.paths.get_global_db_path",
                    return_value=gdb,
                ),
                patch(
                    "cursor_saves.cli.paths.find_all_matching_workspaces",
                    return_value=[ws],
                ),
                patch(
                    "cursor_saves.cli.paths.get_workspace_composer_ids",
                    return_value=[COMPOSER_ID],
                ),
                patch("cursor_saves.cli.db.backup_db"),
                patch("cursor_saves.cli._load_sync_state", return_value={}),
                patch("cursor_saves.cli._save_sync_state"),
                patch(
                    "cursor_saves.cli._get_sync_state_path",
                    return_value=root / "sync_state.json",
                ),
            ):
                _pull_behind(root / "sync")

            return imported

    def test_skips_when_blob_counts_zero_and_no_plans(self):
        meta = {
            "composerId": COMPOSER_ID,
            "messageCount": 1,
            "planCount": None,
            "contentBlobCount": None,
            "agentBlobCount": None,
        }
        # Keys present (null → 0) → short-circuit skip even if probe would be True
        imported = self._run_queue_collect(meta, missing=True)
        self.assertEqual(imported, [])

    def test_queues_when_probe_finds_missing(self):
        meta = {
            "composerId": COMPOSER_ID,
            "messageCount": 1,
            "planCount": None,
            "contentBlobCount": 1,
            "agentBlobCount": None,
        }
        imported = self._run_queue_collect(meta, missing=True)
        self.assertEqual(len(imported), 1)

    def test_skips_when_counts_positive_but_probe_clean(self):
        meta = {
            "composerId": COMPOSER_ID,
            "messageCount": 1,
            "contentBlobCount": 2,
            "agentBlobCount": 0,
            "planCount": None,
        }
        imported = self._run_queue_collect(meta, missing=False)
        self.assertEqual(imported, [])

    def test_legacy_meta_probes(self):
        meta = {
            "composerId": COMPOSER_ID,
            "messageCount": 1,
            # no contentBlobCount / agentBlobCount keys
            "planCount": None,
        }
        imported = self._run_queue_collect(meta, missing=True)
        self.assertEqual(len(imported), 1)


class TestReadSnapshotMetaBlobCounts(unittest.TestCase):
    def test_fallback_includes_blob_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / f"{COMPOSER_ID}.json.gz"
            _write_snapshot(
                snap,
                content_blobs={CONTENT_HASH: "x"},
                agent_blobs={"e" * 64: base64.b64encode(b"z").decode("ascii")},
            )
            meta = importer_mod.read_snapshot_meta(snap)
            self.assertEqual(meta.get("contentBlobCount"), 1)
            self.assertEqual(meta.get("agentBlobCount"), 1)


if __name__ == "__main__":
    unittest.main()
