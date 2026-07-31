"""Tests for agent-config pack sync (v0.9.13)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cursor_saves import agent_config_sync as acs
from cursor_saves.backends import GitBackend


class TestDangerousSkip(unittest.TestCase):
    def test_skips_env_and_pem(self):
        self.assertTrue(acs._is_dangerous_name(".env"))
        self.assertTrue(acs._is_dangerous_name(".env.local"))
        self.assertTrue(acs._is_dangerous_name("key.pem"))
        self.assertTrue(acs._is_dangerous_name("id_rsa.key"))
        self.assertTrue(acs._is_dangerous_name("gcp-credentials.json"))
        self.assertFalse(acs._is_dangerous_name("SKILL.md"))
        self.assertFalse(acs._is_dangerous_name("rule.mdc"))


class TestPersonalRoundtrip(unittest.TestCase):
    def test_export_import_equal_content_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync = tmp_path / "sync"
            home = tmp_path / "home"
            cursor = home / ".cursor"
            skill = cursor / "skills" / "demo" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("hello skill\n", encoding="utf-8")
            (cursor / "rules").mkdir()
            (cursor / "rules" / "x.mdc").write_text("rule\n", encoding="utf-8")
            (cursor / "skills" / "demo" / ".env").write_text("SECRET=1\n", encoding="utf-8")

            with (
                patch.object(acs.paths, "get_sync_dir", return_value=sync),
                patch.object(acs, "_personal_cursor_home", return_value=cursor),
                patch.object(acs, "_detect_wsl_personal_cursor", return_value=None),
            ):
                n = acs.export_personal()
                self.assertGreaterEqual(n, 2)
                packed = sync / "agent-config" / "personal" / "skills" / "demo" / "SKILL.md"
                self.assertTrue(packed.is_file())
                self.assertFalse(
                    (sync / "agent-config" / "personal" / "skills" / "demo" / ".env").exists()
                )

                # Second export: equal content → 0 writes
                self.assertEqual(acs.export_personal(), 0)

                # Import into a fresh home
                dest = tmp_path / "home2" / ".cursor"
                with patch.object(acs, "_personal_cursor_home", return_value=dest):
                    imported = acs.import_personal()
                self.assertGreaterEqual(imported, 2)
                self.assertEqual(
                    (dest / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8"),
                    "hello skill\n",
                )
                # Equal content skip on re-import
                with patch.object(acs, "_personal_cursor_home", return_value=dest):
                    self.assertEqual(acs.import_personal(), 0)


class TestProjectGitTrackedSkip(unittest.TestCase):
    def test_skips_tracked_includes_untracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync = tmp_path / "sync"
            proj = tmp_path / "repo"
            proj.mkdir()
            subprocess.run(["git", "init"], cwd=str(proj), check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "t@example.com"],
                cwd=str(proj), check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "t"],
                cwd=str(proj), check=True, capture_output=True,
            )
            tracked = proj / ".cursor" / "rules" / "tracked.mdc"
            tracked.parent.mkdir(parents=True)
            tracked.write_text("tracked\n", encoding="utf-8")
            subprocess.run(["git", "add", ".cursor/rules/tracked.mdc"], cwd=str(proj), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "t"], cwd=str(proj), check=True, capture_output=True)

            local_skill = proj / ".cursor" / "skills" / "local" / "SKILL.md"
            local_skill.parent.mkdir(parents=True)
            local_skill.write_text("local only\n", encoding="utf-8")
            (proj / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
            # AGENTS.md untracked → included

            with (
                patch.object(acs.paths, "get_sync_dir", return_value=sync),
                patch.object(acs.paths, "get_project_identifier", return_value="demo-proj"),
            ):
                n = acs.export_project(str(proj))
                self.assertGreaterEqual(n, 2)
                base = sync / "agent-config" / "projects" / "demo-proj"
                self.assertTrue((base / ".cursor" / "skills" / "local" / "SKILL.md").is_file())
                self.assertTrue((base / "AGENTS.md").is_file())
                self.assertFalse((base / ".cursor" / "rules" / "tracked.mdc").exists())

                # Import must not overwrite tracked file even if present in pack
                (base / ".cursor" / "rules").mkdir(parents=True, exist_ok=True)
                (base / ".cursor" / "rules" / "tracked.mdc").write_text("from pack\n", encoding="utf-8")
                tracked.write_text("tracked\n", encoding="utf-8")
                acs.import_project(str(proj))
                self.assertEqual(tracked.read_text(encoding="utf-8"), "tracked\n")


class TestGitBackendStagesAgentConfig(unittest.TestCase):
    def test_push_stages_agent_config_with_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(sync), check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "t@example.com"],
                cwd=str(sync), check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "t"],
                cwd=str(sync), check=True, capture_output=True,
            )
            (sync / "snapshots").mkdir()
            snap = sync / "snapshots" / "p" / "c.json.gz"
            snap.parent.mkdir(parents=True)
            snap.write_bytes(b"gz")
            agent = sync / "agent-config" / "personal" / "skills" / "a" / "SKILL.md"
            agent.parent.mkdir(parents=True)
            agent.write_text("x\n", encoding="utf-8")

            be = GitBackend(sync)
            with patch.object(be, "_push_origin_only", return_value=True):
                self.assertTrue(be.push(sync / "snapshots", paths=[snap]))

            # Committed tree should include agent-config
            ls = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "HEAD"],
                cwd=str(sync), capture_output=True, encoding="utf-8", check=True,
            )
            names = ls.stdout.splitlines()
            self.assertTrue(any(n.startswith("agent-config/") for n in names))
            self.assertTrue(any(n.startswith("snapshots/") for n in names))

    def test_push_full_without_agent_config_still_commits_snapshots(self):
        """Regression: missing agent-config/ must not break snapshot push."""
        with tempfile.TemporaryDirectory() as tmp:
            sync = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(sync), check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "t@example.com"],
                cwd=str(sync), check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "t"],
                cwd=str(sync), check=True, capture_output=True,
            )
            snap = sync / "snapshots" / "p" / "c.json.gz"
            snap.parent.mkdir(parents=True)
            snap.write_bytes(b"gz")
            self.assertFalse((sync / "agent-config").exists())

            be = GitBackend(sync)
            with patch.object(be, "_push_origin_only", return_value=True):
                self.assertTrue(be.push(sync / "snapshots", paths=None))

            ls = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "HEAD"],
                cwd=str(sync), capture_output=True, encoding="utf-8", check=True,
            )
            names = ls.stdout.splitlines()
            self.assertTrue(any(n.startswith("snapshots/") for n in names))
            self.assertFalse(any(n.startswith("agent-config/") for n in names))

    def test_push_full_adds_agent_config_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync = Path(tmp)
            be = GitBackend(sync)
            (sync / "agent-config").mkdir()
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch("cursor_saves.backends.subprocess.run", side_effect=fake_run),
                patch.object(be, "_push_origin_only", return_value=True),
            ):
                be.push(sync / "snapshots", paths=None)

            add_calls = [c for c in calls if c[:2] == ["git", "add"]]
            self.assertTrue(any(c == ["git", "add", "snapshots/"] for c in add_calls))
            self.assertTrue(any(c == ["git", "add", "agent-config/"] for c in add_calls))
            # Must be separate adds — combined pathspec fails when agent-config missing
            self.assertFalse(any("snapshots/" in c and "agent-config/" in c for c in add_calls))


class TestImportProjectAllowlist(unittest.TestCase):
    def test_skips_disallowed_paths_and_dead_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync = tmp_path / "sync"
            proj = tmp_path / "proj"
            proj.mkdir()
            base = sync / "agent-config" / "projects" / "demo-proj"
            allowed = base / ".cursor" / "skills" / "s" / "SKILL.md"
            allowed.parent.mkdir(parents=True)
            allowed.write_text("ok\n", encoding="utf-8")
            (base / "AGENTS.md").write_text("# a\n", encoding="utf-8")
            bad = base / "secrets" / "token.txt"
            bad.parent.mkdir(parents=True)
            bad.write_text("nope\n", encoding="utf-8")
            (base / ".cursor" / "mcp.json").write_text("{}\n", encoding="utf-8")

            with (
                patch.object(acs.paths, "get_sync_dir", return_value=sync),
                patch.object(acs.paths, "get_project_identifier", return_value="demo-proj"),
            ):
                n = acs.import_project(str(proj))
                self.assertGreaterEqual(n, 2)
                self.assertTrue((proj / ".cursor" / "skills" / "s" / "SKILL.md").is_file())
                self.assertTrue((proj / "AGENTS.md").is_file())
                self.assertFalse((proj / "secrets" / "token.txt").exists())
                self.assertFalse((proj / ".cursor" / "mcp.json").exists())

                dead = tmp_path / "missing-project"
                self.assertFalse(dead.exists())
                self.assertEqual(acs.import_project(str(dead)), 0)
                self.assertFalse(dead.exists())


class TestWslDetect(unittest.TestCase):
    def test_empty_distro_name_undetectable(self):
        home = MagicMock(returncode=0, stdout="/home/user\n", stderr="")
        distro = MagicMock(returncode=0, stdout="", stderr="")

        def fake_run(cmd, **kwargs):
            joined = " ".join(cmd)
            if "$HOME" in joined:
                return home
            return distro

        with (
            patch("cursor_saves.agent_config_sync.platform.system", return_value="Windows"),
            patch("cursor_saves.agent_config_sync.subprocess.run", side_effect=fake_run),
        ):
            self.assertIsNone(acs._detect_wsl_personal_cursor())

    def test_wsl_mirror_error_does_not_abort_personal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync = tmp_path / "sync"
            cursor = tmp_path / "home" / ".cursor"
            src = sync / "agent-config" / "personal" / "skills" / "d" / "SKILL.md"
            src.parent.mkdir(parents=True)
            src.write_text("hi\n", encoding="utf-8")
            wsl = tmp_path / "wsl-cursor"
            acs._wsl_log_done = False

            with (
                patch.object(acs.paths, "get_sync_dir", return_value=sync),
                patch.object(acs, "_personal_cursor_home", return_value=cursor),
                patch.object(acs, "_detect_wsl_personal_cursor", return_value=wsl),
                patch.object(acs, "_sync_tree", side_effect=[1, OSError("wsl dead")]),
            ):
                # First _sync_tree = Windows personal; second = WSL (raises)
                n = acs.import_personal()
            self.assertEqual(n, 1)

    def test_export_includes_wsl_and_overlays_on_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync = tmp_path / "sync"
            win = tmp_path / "win" / ".cursor"
            wsl = tmp_path / "wsl" / ".cursor"
            win_skill = win / "skills" / "shared" / "SKILL.md"
            win_skill.parent.mkdir(parents=True)
            win_skill.write_text("windows\n", encoding="utf-8")
            (win / "skills" / "win-only" / "SKILL.md").parent.mkdir(parents=True)
            (win / "skills" / "win-only" / "SKILL.md").write_text("win only\n", encoding="utf-8")
            wsl_skill = wsl / "skills" / "shared" / "SKILL.md"
            wsl_skill.parent.mkdir(parents=True)
            wsl_skill.write_text("wsl wins\n", encoding="utf-8")
            (wsl / "skills" / "wsl-only" / "SKILL.md").parent.mkdir(parents=True)
            (wsl / "skills" / "wsl-only" / "SKILL.md").write_text("wsl only\n", encoding="utf-8")
            acs._wsl_log_done = False

            with (
                patch.object(acs.paths, "get_sync_dir", return_value=sync),
                patch.object(acs, "_personal_cursor_home", return_value=win),
                patch.object(acs, "_detect_wsl_personal_cursor", return_value=wsl),
            ):
                n = acs.export_personal()
            self.assertGreaterEqual(n, 3)
            base = sync / "agent-config" / "personal" / "skills"
            self.assertEqual(
                (base / "shared" / "SKILL.md").read_text(encoding="utf-8"),
                "wsl wins\n",
            )
            self.assertTrue((base / "win-only" / "SKILL.md").is_file())
            self.assertTrue((base / "wsl-only" / "SKILL.md").is_file())

    def test_export_wsl_error_does_not_abort_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync = tmp_path / "sync"
            win = tmp_path / "win" / ".cursor"
            skill = win / "skills" / "d" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("win\n", encoding="utf-8")
            wsl = tmp_path / "wsl-cursor"
            (wsl / "skills").mkdir(parents=True)
            acs._wsl_log_done = False

            with (
                patch.object(acs.paths, "get_sync_dir", return_value=sync),
                patch.object(acs, "_personal_cursor_home", return_value=win),
                patch.object(acs, "_detect_wsl_personal_cursor", return_value=wsl),
                patch.object(
                    acs,
                    "_sync_tree",
                    side_effect=[1, OSError("wsl dead")],
                ),
            ):
                n = acs.export_personal()
            self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
