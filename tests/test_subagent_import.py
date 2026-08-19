"""Subagent chats must not appear as top-level workspace conversations after sync."""

from __future__ import annotations

import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_saves import db, importer as importer_mod

PARENT_ID = "11111111-1111-1111-1111-111111111111"
CHILD_ID = "task-toolu-child000000000000000000000001"
BUBBLE_ID = "22222222-2222-2222-2222-222222222222"


def _init_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE, value BLOB)")
    conn.execute("CREATE TABLE IF NOT EXISTS cursorDiskKV (key TEXT UNIQUE, value BLOB)")
    conn.commit()
    conn.close()


class TestSubagentImport(unittest.TestCase):
    def test_is_subagent_composer(self):
        self.assertTrue(
            importer_mod._is_subagent_composer(
                CHILD_ID, {"isSubagent": True}
            )
        )
        self.assertTrue(
            importer_mod._is_subagent_composer(
                CHILD_ID, {}
            )
        )
        self.assertFalse(
            importer_mod._is_subagent_composer(
                PARENT_ID, {"name": "parent"}
            )
        )

    def test_header_entry_includes_is_subagent(self):
        entry = importer_mod._build_composer_header_entry(
            CHILD_ID, {"isSubagent": True, "name": "explore"}
        )
        self.assertTrue(entry.get("isSubagent"))

    def test_register_subagent_skips_selected_composer_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws_dir = root / "ws"
            ws_dir.mkdir()
            ws_db = ws_dir / "state.vscdb"
            _init_db(ws_db)
            with db.CursorDB(ws_db) as cdb:
                cdb.write_json(
                    "composer.composerData",
                    {
                        "selectedComposerIds": [CHILD_ID, PARENT_ID],
                        "allComposers": [{"composerId": CHILD_ID, "name": "bad"}],
                    },
                    table="ItemTable",
                )

            gdb = root / "global.vscdb"
            _init_db(gdb)

            child_data = {
                "name": "explore task",
                "isSubagent": True,
                "fullConversationHeadersOnly": [{"bubbleId": BUBBLE_ID}],
            }

            with (
                patch.object(importer_mod.paths, "get_global_db_path", return_value=gdb),
                patch.object(importer_mod, "_register_in_global_headers") as reg_headers,
                patch.object(
                    importer_mod,
                    "_link_subagent_to_parent_from_snapshots",
                    return_value=False,
                ),
            ):
                ok = importer_mod._register_in_workspace(
                    CHILD_ID, child_data, ws_dir, project_identifier="proj"
                )

            self.assertTrue(ok)
            reg_headers.assert_called_once()
            with db.CursorDB(ws_db) as cdb:
                data = cdb.get_json("composer.composerData", table="ItemTable")
                self.assertNotIn(CHILD_ID, data.get("selectedComposerIds", []))
                self.assertNotIn(
                    CHILD_ID,
                    {c.get("composerId") for c in data.get("allComposers", [])},
                )
                self.assertIn(PARENT_ID, data.get("selectedComposerIds", []))

    def test_link_subagent_updates_parent_sub_composer_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gdb = root / "state.vscdb"
            snaps = root / "snapshots" / "proj"
            snaps.mkdir(parents=True)
            _init_db(gdb)

            parent_data = {
                "name": "parent",
                "subComposerIds": [CHILD_ID],
                "fullConversationHeadersOnly": [{"bubbleId": BUBBLE_ID}],
            }
            with db.CursorDB(gdb) as cdb:
                cdb.write_json(f"composerData:{PARENT_ID}", dict(parent_data))

            parent_snap = {
                "version": 3,
                "composerId": PARENT_ID,
                "projectIdentifier": "proj",
                "composerData": parent_data,
                "contentBlobs": {},
                "bubbleEntries": {},
                "agentBlobs": {},
            }
            (snaps / f"{PARENT_ID}.json.gz").write_bytes(
                gzip.compress(json.dumps(parent_snap).encode("utf-8"))
            )

            # Parent locally missing child reference (stale after partial import)
            with db.CursorDB(gdb) as cdb:
                stale = cdb.get_json(f"composerData:{PARENT_ID}")
                stale["subComposerIds"] = []
                cdb.write_json(f"composerData:{PARENT_ID}", stale)

            with (
                patch.object(importer_mod.paths, "get_global_db_path", return_value=gdb),
                patch.object(importer_mod.paths, "get_snapshots_dir", return_value=root / "snapshots"),
            ):
                with db.CursorDB(gdb) as cdb:
                    linked = importer_mod._link_subagent_to_parent_from_snapshots(
                        cdb, CHILD_ID, "proj"
                    )

            self.assertTrue(linked)
            with db.CursorDB(gdb) as cdb:
                updated = cdb.get_json(f"composerData:{PARENT_ID}")
                self.assertIn(CHILD_ID, updated.get("subComposerIds", []))


if __name__ == "__main__":
    unittest.main()
