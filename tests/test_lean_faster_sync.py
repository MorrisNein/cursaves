"""Focused unit tests for lean faster sync (v0.9.11).

Does not touch real ~/.cursaves — uses tempdirs and mocks for git/subprocess.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cursor_saves.backends import GitBackend
from cursor_saves.cli import (
    _ensure_synced,
    _export_and_push,
    _maybe_advance_pull_tip,
    _remote_delta_fully_covered,
)
from cursor_saves.export import _collect_content_hashes, checkpoint_project
from cursor_saves import importer as importer_mod


class TestDirtyUnpushedRefuse(unittest.TestCase):
    def test_dirty_worktree_refuses_reset(self):
        be = GitBackend(Path("/tmp/fake-cursaves"))
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(be, "_worktree_dirty", return_value=True),
            patch.object(be, "has_unpushed_commits", return_value=False),
            patch("cursor_saves.backends.subprocess.run") as run,
        ):
            self.assertFalse(be._reset_to_origin(previous_tip="abc"))
            run.assert_not_called()

    def test_unpushed_commits_refuse_reset(self):
        be = GitBackend(Path("/tmp/fake-cursaves"))
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(be, "_worktree_dirty", return_value=False),
            patch.object(be, "has_unpushed_commits", return_value=True),
            patch("cursor_saves.backends.subprocess.run") as run,
        ):
            self.assertFalse(be._reset_to_origin(previous_tip="abc"))
            run.assert_not_called()

    def test_has_unpushed_failsafe_true_on_rev_list_error(self):
        be = GitBackend(Path("/tmp/fake-cursaves"))
        with (
            patch.object(be, "has_remote", return_value=True),
            patch(
                "cursor_saves.backends.subprocess.run",
                return_value=MagicMock(returncode=128, stdout="", stderr="fatal"),
            ),
        ):
            self.assertTrue(be.has_unpushed_commits())


class TestTipAdvancement(unittest.TestCase):
    def test_ensure_synced_does_not_advance_tip(self):
        backend = MagicMock(spec=GitBackend)
        backend.has_remote.return_value = True
        backend.last_pull_tip = "newtip"
        with (
            patch("cursor_saves.cli.paths.is_sync_repo_initialized", return_value=True),
            patch("cursor_saves.cli.get_backend", return_value=backend),
            patch("cursor_saves.cli.paths.get_snapshots_dir", return_value=Path("/tmp/s")),
            patch("cursor_saves.cli._load_sync_state", return_value={"lastSuccessfulRemoteTip": "old"}),
            patch("cursor_saves.cli._mark_pull_tip") as mark,
            patch("cursor_saves.cli.GitBackend", GitBackend),
        ):
            # isinstance check needs real GitBackend instance
            real = GitBackend(Path("/tmp/fake"))
            real.has_remote = MagicMock(return_value=True)
            real.pull = MagicMock(return_value=True)
            real.last_pull_tip = "newtip"
            with patch("cursor_saves.cli.get_backend", return_value=real):
                _ensure_synced()
            mark.assert_not_called()
            real.pull.assert_called_once()

    def test_maybe_advance_skips_on_failure(self):
        with patch("cursor_saves.cli._mark_pull_tip") as mark:
            _maybe_advance_pull_tip(
                "tip1", failure=1, changed_ids=set(), project_path=None
            )
            mark.assert_not_called()

    def test_maybe_advance_on_empty_delta_success(self):
        with patch("cursor_saves.cli._mark_pull_tip") as mark:
            _maybe_advance_pull_tip(
                "tip1", failure=0, changed_ids=set(), project_path=None
            )
            mark.assert_called_once_with("tip1")

    def test_maybe_advance_skips_scoped_when_delta_outside_project(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            other = root / "other-proj"
            other.mkdir()
            (other / "cid-other.meta.json").write_text("{}")
            with (
                patch("cursor_saves.cli.paths.get_snapshots_dir", return_value=root),
                patch(
                    "cursor_saves.cli.paths.get_project_identifier",
                    return_value="my-proj",
                ),
                patch("cursor_saves.cli._mark_pull_tip") as mark,
            ):
                (root / "my-proj").mkdir()
                self.assertFalse(
                    _remote_delta_fully_covered(
                        {"cid-other"}, project_path="/x/my"
                    )
                )
                _maybe_advance_pull_tip(
                    "tip1",
                    failure=0,
                    changed_ids={"cid-other"},
                    project_path="/x/my",
                )
                mark.assert_not_called()

    def test_maybe_advance_when_scoped_delta_fully_in_project(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mine = root / "my-proj"
            mine.mkdir()
            (mine / "cid-a.meta.json").write_text("{}")
            with (
                patch("cursor_saves.cli.paths.get_snapshots_dir", return_value=root),
                patch(
                    "cursor_saves.cli.paths.get_project_identifier",
                    return_value="my-proj",
                ),
                patch("cursor_saves.cli._mark_pull_tip") as mark,
            ):
                _maybe_advance_pull_tip(
                    "tip1",
                    failure=0,
                    changed_ids={"cid-a"},
                    project_path="/x/my",
                )
                mark.assert_called_once_with("tip1")

    def test_unknown_delta_not_covered_when_scoped(self):
        self.assertFalse(
            _remote_delta_fully_covered(None, project_path="/some/project")
        )
        self.assertTrue(_remote_delta_fully_covered(None, project_path=None))


class TestDeltaComposerIds(unittest.TestCase):
    def test_extract_from_meta_gz_and_shards(self):
        paths = [
            "snapshots/proj/aaa.meta.json",
            "snapshots/proj/bbb.json.gz",
            "snapshots/proj/ccc.json.gz.00",
            "snapshots/proj/ccc.json.gz.01",
            "snapshots/proj/readme.txt",
            "snapshots/proj/ddd.json",  # ignored (no .json.gz / .meta)
        ]
        ids = GitBackend._composer_ids_from_diff_paths(paths)
        self.assertEqual(ids, {"aaa", "bbb", "ccc"})


class TestCandidateImportFilter(unittest.TestCase):
    def test_non_candidates_never_call_import_snapshot(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            for cid in ("keep-me", "skip-me"):
                (snap_dir / f"{cid}.meta.json").write_text(
                    json.dumps({"composerId": cid, "messageCount": 1, "name": cid})
                )
                (snap_dir / f"{cid}.json.gz").write_bytes(b"not-real-gzip")

            imported: list[str] = []

            def fake_import(path, *args, **kwargs):
                imported.append(Path(path).name.split(".")[0])
                return True

            with (
                patch.object(importer_mod, "is_cursor_running", return_value=False),
                patch.object(importer_mod, "list_snapshot_files") as list_sf,
                patch.object(importer_mod, "read_snapshot_meta") as read_meta,
                patch.object(importer_mod, "import_snapshot", side_effect=fake_import),
                patch.object(importer_mod.db, "backup_db"),
                patch.object(
                    importer_mod,
                    "find_or_create_workspace",
                    return_value=snap_dir,
                ),
                patch.object(
                    importer_mod.paths,
                    "get_global_db_path",
                    return_value=snap_dir / "missing.vscdb",
                ),
            ):
                files = [
                    snap_dir / "keep-me.json.gz",
                    snap_dir / "skip-me.json.gz",
                ]
                list_sf.return_value = files

                def _meta(sf):
                    cid = sf.name.split(".")[0]
                    return {"composerId": cid, "messageCount": 3, "name": cid}

                read_meta.side_effect = _meta

                success, failure = importer_mod.import_from_snapshot_dir(
                    snap_dir,
                    "/tmp/project",
                    force=True,
                    composer_ids={"keep-me"},
                )

            self.assertEqual(success, 1)
            self.assertEqual(failure, 0)
            self.assertEqual(imported, ["keep-me"])
            # read_snapshot_meta used for filter; import_snapshot only for candidate
            self.assertEqual(read_meta.call_count, 2)


class TestContentHashCollector(unittest.TestCase):
    def test_collects_hashes_from_composer_data_and_bubbles(self):
        h1 = "a" * 64
        h2 = "b" * 64
        h3 = "c" * 64
        composer_data = {
            "name": "chat",
            "context": {"fileHash": h1},
            "nested": [{"contentHash": h2}],
        }
        bubbles = {
            "b1": {"text": "hi", "attachment": h3},
            "b2": {"note": "not-a-hash"},
        }
        found = _collect_content_hashes(composer_data, bubbles)
        self.assertEqual(found, {h1, h2, h3})

    def test_ignores_short_hex(self):
        found = _collect_content_hashes({"x": "abcdef0123456789"})
        self.assertEqual(found, set())


class TestSkipEmptyBulkOnly(unittest.TestCase):
    def _run_checkpoint(self, composer_ids, conversations, cds):
        saved_ids: list[str] = []

        class FakeCDB:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_json(self, key):
                cid = key.split(":", 1)[1]
                return cds.get(cid, {})

        def fake_export(project_path, composer_id, _cdb=None, source_host=None):
            return {"composerId": composer_id}

        def fake_save(snapshot, snapshots_dir):
            saved_ids.append(snapshot["composerId"])
            return Path(f"/tmp/{snapshot['composerId']}.json.gz")

        with (
            patch(
                "cursor_saves.export.get_workspace_conversations",
                return_value=conversations,
            ),
            patch("cursor_saves.export.paths.get_snapshots_dir", return_value=Path("/tmp")),
            patch(
                "cursor_saves.export.paths.get_global_db_path",
                return_value=Path("/tmp/g.vscdb"),
            ),
            patch("cursor_saves.export.db.CursorDB", FakeCDB),
            patch("cursor_saves.export.export_conversation", side_effect=fake_export),
            patch("cursor_saves.export.save_snapshot", side_effect=fake_save),
        ):
            checkpoint_project("/proj", composer_ids=composer_ids)
        return saved_ids

    def test_bulk_skips_empty_unnamed(self):
        conversations = [
            {"composerId": "empty1", "name": ""},
            {"composerId": "named1", "name": "Hello"},
        ]
        cds = {
            "empty1": {"name": "", "fullConversationHeadersOnly": []},
            "named1": {"name": "Hello", "fullConversationHeadersOnly": [{"bubbleId": "x"}]},
        }
        saved = self._run_checkpoint(None, conversations, cds)
        self.assertEqual(saved, ["named1"])

    def test_explicit_ids_do_not_skip_empty(self):
        conversations = [
            {"composerId": "empty1", "name": ""},
            {"composerId": "named1", "name": "Hello"},
        ]
        cds = {
            "empty1": {"name": "", "fullConversationHeadersOnly": []},
            "named1": {"name": "Hello", "fullConversationHeadersOnly": [{"bubbleId": "x"}]},
        }
        saved = self._run_checkpoint(["empty1", "named1"], conversations, cds)
        self.assertEqual(saved, ["empty1", "named1"])


class TestChunkedPushStaging(unittest.TestCase):
    def test_push_with_paths_stages_only_batch_and_pushes_pending_first(self):
        sync_dir = Path("/tmp/fake-sync")
        be = GitBackend(sync_dir)
        batch = [Path("/tmp/fake-sync/snapshots/p/a.json.gz")]

        staged: list[list] = []
        pending_pushed = {"n": 0}

        def fake_run(cmd, **kwargs):
            # Track git add calls
            if cmd[:2] == ["git", "add"] and "--" in cmd:
                staged.append(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            return m

        with (
            patch.object(be, "has_remote", return_value=True),
            patch.object(be, "_stage_snapshot_paths") as stage,
            patch.object(be, "_push_origin_only", return_value=True) as push_only,
            patch(
                "cursor_saves.backends.subprocess.run",
                side_effect=fake_run,
            ),
            patch(
                "cursor_saves.backends.git_commit_env",
                return_value={},
            ),
        ):
            # No staged changes → returncode 0 on diff --cached --quiet → skip commit
            ok = be.push(Path("/tmp/fake-sync/snapshots"), paths=batch)
            self.assertTrue(ok)
            stage.assert_called_once_with(batch)
            push_only.assert_called_once()

        # Pending-first via _export_and_push
        backend = MagicMock(spec=GitBackend)
        backend.has_remote.return_value = True
        backend.has_unpushed_commits.return_value = True
        backend._push_origin_only.return_value = True
        backend.push.return_value = True

        items = [
            {
                "composerId": f"c{i}",
                "project_path": "/p",
                "workspace_dir": Path("/ws"),
                "host": None,
                "name": f"n{i}",
                "workspace_label": "p",
            }
            for i in range(3)
        ]

        with (
            patch("cursor_saves.cli.GitBackend", GitBackend),
            patch("cursor_saves.cli.paths.get_snapshots_dir", return_value=Path("/s")),
            patch("cursor_saves.cli.export.checkpoint_project", return_value=[Path("/s/a.json.gz")]),
            patch("cursor_saves.cli.get_backend", return_value=backend),
        ):
            # isinstance(backend, GitBackend) is False for MagicMock(spec=...)
            # Use a real instance with mocked methods instead
            real = GitBackend(Path("/tmp/fake-sync"))
            real.has_remote = MagicMock(return_value=True)
            real.has_unpushed_commits = MagicMock(return_value=True)
            real._push_origin_only = MagicMock(return_value=True)
            real.push = MagicMock(return_value=True)

            with (
                patch("cursor_saves.cli.paths.get_snapshots_dir", return_value=Path("/s")),
                patch(
                    "cursor_saves.cli.export.checkpoint_project",
                    return_value=[Path("/s/a.json.gz")],
                ),
                patch("cursor_saves.cli._export_agent_config", return_value=0),
                patch("cursor_saves.cli._push_agent_config", return_value=True),
            ):
                n = _export_and_push(
                    Path("/tmp/fake-sync"),
                    items,
                    backend=real,
                    chunk_size=2,
                )

            real._push_origin_only.assert_called()  # pending first
            # Then push called with batch paths (not full tree)
            self.assertTrue(real.push.called)
            for c in real.push.call_args_list:
                self.assertIn("paths", c.kwargs)
                self.assertIsInstance(c.kwargs["paths"], list)
            self.assertGreater(n, 0)


if __name__ == "__main__":
    unittest.main()
