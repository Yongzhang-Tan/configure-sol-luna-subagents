from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "configure-sol-luna-subagents" / "scripts" / "configure.py"


class ConfigureScriptTests(unittest.TestCase):
    def run_tool(self, home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments, "--codex-home", str(home)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_empty_home_apply_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "empty-home"
            applied = self.run_tool(home, "apply")
            self.assertEqual(applied.returncode, 0, applied.stderr)
            verified = self.run_tool(home, "verify")
            self.assertEqual(verified.returncode, 0, verified.stderr)
            config = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "gpt-5.6-sol"', config)
            self.assertNotIn("max_threads", config)
            self.assertTrue((home / "AGENTS.md").is_file())
            self.assertTrue((home / "agents" / "sol_luna_code_mapper.toml").is_file())

    def test_unrelated_config_and_comments_are_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            original = (
                "# retain this comment\n"
                'model = "old-model" # target\n'
                'model_reasoning_effort = "low"\n'
                'model_provider = "retain-provider"\n\n'
                "[agents]\n"
                "max_threads = 8 # legacy\n"
                'keep = "yes"\n\n'
                "[features]\n"
                "multi_agent_v2 = true\n"
            )
            (home / "config.toml").write_text(original, encoding="utf-8")
            result = self.run_tool(home, "apply")
            self.assertEqual(result.returncode, 0, result.stderr)
            updated = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn("# retain this comment", updated)
            self.assertIn('model_provider = "retain-provider"', updated)
            self.assertIn('keep = "yes"', updated)
            self.assertIn("multi_agent_v2 = true", updated)
            self.assertNotIn("max_threads", updated)

    def test_existing_config_without_agents_gets_appended_section(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text(
                '# keep\nmodel = "old-model"\n\n[features]\nkeep = true\n',
                encoding="utf-8",
            )
            result = self.run_tool(home, "apply")
            self.assertEqual(result.returncode, 0, result.stderr)
            config = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn("[agents]\n", config)
            self.assertIn("keep = true", config)

    def test_managed_agents_update_and_apply_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            first = self.run_tool(home, "apply")
            self.assertEqual(first.returncode, 0, first.stderr)
            agent = home / "agents" / "sol_luna_code_mapper.toml"
            changed = agent.read_text(encoding="utf-8").replace(
                'model = "gpt-5.6-luna"', 'model = "old-model"'
            )
            agent.write_text(changed, encoding="utf-8")
            second = self.run_tool(home, "apply")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn('model = "gpt-5.6-luna"', agent.read_text(encoding="utf-8"))
            snapshot = {
                str(path.relative_to(home)): path.read_bytes()
                for path in [
                    home / "config.toml",
                    home / "AGENTS.md",
                    home / "agents" / "sol_luna_code_mapper.toml",
                    home / "agents" / "sol_luna_implementation_worker.toml",
                ]
            }
            third = self.run_tool(home, "apply")
            self.assertEqual(third.returncode, 0, third.stderr)
            for relative, content in snapshot.items():
                self.assertEqual((home / relative).read_bytes(), content)

    def test_unmanaged_agent_collision_aborts_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            agent_dir = home / "agents"
            agent_dir.mkdir(parents=True)
            collision = agent_dir / "sol_luna_code_mapper.toml"
            collision.write_text('name = "someone_else"\n', encoding="utf-8")
            result = self.run_tool(home, "apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(collision.read_text(encoding="utf-8"), 'name = "someone_else"\n')
            self.assertFalse((home / "config.toml").exists())

    def test_invalid_toml_aborts_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.toml"
            config.write_text("[agents\n", encoding="utf-8")
            before = config.read_bytes()
            result = self.run_tool(home, "apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse((home / "agents").exists())

    def test_multiline_root_target_aborts_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.toml"
            config.write_text(
                'model = [\n  "old-model",\n  "another-model",\n]\n',
                encoding="utf-8",
            )
            before = config.read_bytes()
            result = self.run_tool(home, "apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse((home / "agents").exists())

    def test_multiline_agents_target_aborts_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.toml"
            config.write_text(
                '[agents]\ndefault_subagent_model = [\n  "old-model",\n]\n',
                encoding="utf-8",
            )
            before = config.read_bytes()
            result = self.run_tool(home, "apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse((home / "agents").exists())

    def test_backup_and_rollback_restore_exact_manifest_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.toml"
            original = '# keep\nmodel = "before"\n'
            config.write_text(original, encoding="utf-8")
            applied = self.run_tool(home, "apply")
            self.assertEqual(applied.returncode, 0, applied.stderr)
            backup_line = next(
                line for line in applied.stdout.splitlines() if line.startswith("backup: ")
            )
            backup = backup_line.split(": ", 1)[1]
            config.write_text('model = "tampered"\n', encoding="utf-8")
            rolled = self.run_tool(home, "rollback", "--backup", backup)
            self.assertEqual(rolled.returncode, 0, rolled.stderr)
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertFalse((home / "AGENTS.md").exists())
            self.assertFalse((home / "agents" / "sol_luna_code_mapper.toml").exists())
            manifest = json.loads((Path(backup) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["codex_home"], str(home.resolve()))

    def test_non_empty_override_is_active_instruction_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            base = home / "AGENTS.md"
            override = home / "AGENTS.override.md"
            base.write_text("base instructions\n", encoding="utf-8")
            override.write_text("override instructions\n", encoding="utf-8")
            result = self.run_tool(home, "apply")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(base.read_text(encoding="utf-8"), "base instructions\n")
            self.assertIn("BEGIN MANAGED: configure-sol-luna-subagents", override.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
