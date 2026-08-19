"""Collapse must see JSON/selectedComposerIds leftovers, not only native SQL."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_saves import importer as importer_mod

URI = "vscode-remote://wsl+ubuntu/home/petro/verme/verme.code-workspace"
HASH_LIVE = "f05a04c4d33f200262f9107467ba1165"
HASH_GHOST = "a626fef8c73c18154692b607e994e635"


class TestCollapseJsonGhosts(unittest.TestCase):
    def test_list_collapse_groups_includes_json_only_leftovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / HASH_LIVE
            ghost = root / HASH_GHOST
            live.mkdir()
            ghost.mkdir()
            (live / "state.vscdb").write_bytes(b"x")
            (ghost / "state.vscdb").write_bytes(b"x")

            workspaces = [
                {
                    "workspace_dir": live,
                    "folder_uri": URI,
                    "path": "/home/petro/verme/verme.code-workspace",
                    "type": "workspace",
                    "host": "ubuntu",
                },
                {
                    "workspace_dir": ghost,
                    "folder_uri": URI,
                    "path": "/home/petro/verme/verme.code-workspace",
                    "type": "workspace",
                    "host": "ubuntu",
                },
            ]

            def fake_ids(db_path: Path):
                if db_path.parent.name == HASH_GHOST:
                    return ["json-only-ghost"]
                return ["already-on-live"]

            with (
                patch.object(
                    importer_mod.paths, "list_all_workspaces", return_value=workspaces
                ),
                patch.object(
                    importer_mod.paths,
                    "get_workspace_composer_ids",
                    side_effect=fake_ids,
                ),
            ):
                groups = importer_mod.list_collapse_groups()

            self.assertEqual(len(groups), 1)
            by_hash = {m["hash"]: m for m in groups[0]["members"]}
            self.assertEqual(by_hash[HASH_GHOST]["chat_count"], 1)
            self.assertEqual(by_hash[HASH_GHOST]["chat_ids"], ["json-only-ghost"])
            self.assertEqual(by_hash[HASH_LIVE]["chat_count"], 1)
            self.assertEqual(by_hash[HASH_LIVE]["chat_ids"], ["already-on-live"])
