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
    def run_tool(
        self,
        home: Path,
        *arguments: str,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments, "--codex-home", str(home)],
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def apply(self, home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.run_tool(home, "apply", "--yes", *extra)

    def backup_from(self, result: subprocess.CompletedProcess[str]) -> str:
        return next(
            line.split(": ", 1)[1]
            for line in result.stdout.splitlines()
            if line.startswith("backup: ")
        )

    def test_empty_preview_is_nonwriting_and_resolves_allowlisted_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "empty-home"
            result = self.run_tool(home, "preview")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("gpt-6-astra / medium", result.stdout)
            self.assertIn("gpt-5.6-luna / max", result.stdout)
            self.assertIn("no files were written", result.stdout)
            self.assertFalse(home.exists())

    def test_empty_home_apply_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "empty-home"
            applied = self.apply(home)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            verified = self.run_tool(home, "verify")
            self.assertEqual(verified.returncode, 0, verified.stderr)
            config = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "gpt-6-astra"', config)
            self.assertIn('model_reasoning_effort = "medium"', config)
            self.assertNotIn("max_threads", config)
            self.assertTrue((home / "model-tiers.toml").is_file())
            self.assertTrue((home / "agent-tiers.toml").is_file())
            self.assertTrue((home / "agents" / "code_mapper.toml").is_file())
            self.assertTrue((home / "agents" / "implementation_worker.toml").is_file())
            self.assertTrue((home / "agents" / "routine_state_checker.toml").is_file())

    def test_apply_requires_explicit_noninteractive_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "confirm-home"
            result = self.run_tool(home, "apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires --yes", result.stderr)
            self.assertFalse(home.exists())

    def test_legacy_profile_upgrade_preserves_compatibility_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            legacy = self.apply(home, "--profile", "sol-luna")
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            upgraded = self.apply(home)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            config = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "gpt-6-astra"', config)
            self.assertIn('model_reasoning_effort = "medium"', config)
            self.assertTrue((home / "agents" / "sol_luna_code_mapper.toml").is_file())
            self.assertTrue((home / "agents" / "sol_luna_implementation_worker.toml").is_file())
            self.assertFalse((home / "agents" / "implementation_worker.toml").exists())
            self.assertEqual(self.run_tool(home, "verify").returncode, 0)

    def test_switching_to_legacy_keeps_existing_canonical_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.assertEqual(self.apply(home).returncode, 0)
            switched = self.apply(home, "--profile", "sol-luna")
            self.assertEqual(switched.returncode, 0, switched.stderr)
            self.assertTrue((home / "agents" / "implementation_worker.toml").is_file())
            self.assertFalse((home / "agents" / "sol_luna_implementation_worker.toml").exists())
            self.assertEqual(self.run_tool(home, "verify", "--profile", "sol-luna").returncode, 0)

    def test_unrelated_config_comments_features_mcp_and_provider_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            original = (
                "# retain this comment\n"
                'model = "old-model" # target\n'
                'model_reasoning_effort = "low"\n'
                'model_provider = "openai"\n\n'
                "[agents]\n"
                "max_threads = 8 # legacy\n"
                'keep = "yes"\n\n'
                "[features]\n"
                "multi_agent_v2 = true\n\n"
                "[mcp_servers.keep]\n"
                'command = "keep-mcp"\n'
            )
            (home / "config.toml").write_text(original, encoding="utf-8")
            result = self.apply(home)
            self.assertEqual(result.returncode, 0, result.stderr)
            updated = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn("# retain this comment", updated)
            self.assertIn('model_provider = "openai"', updated)
            self.assertIn('keep = "yes"', updated)
            self.assertIn("multi_agent_v2 = true", updated)
            self.assertIn('command = "keep-mcp"', updated)
            self.assertNotIn("max_threads", updated)

    def test_custom_tier_model_is_materialized_by_sync_without_registry_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            first = self.apply(home)
            self.assertEqual(first.returncode, 0, first.stderr)
            registry = home / "model-tiers.toml"
            original = registry.read_text(encoding="utf-8")
            registry.write_text(original.replace("gpt-5.6-luna", "custom-luna"), encoding="utf-8")
            synced = self.run_tool(home, "sync", "--yes")
            self.assertEqual(synced.returncode, 0, synced.stderr)
            self.assertEqual(registry.read_text(encoding="utf-8").count("custom-luna"), 2)
            self.assertIn(
                'default_subagent_model = "custom-luna"',
                (home / "config.toml").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'model = "custom-luna"',
                (home / "agents" / "code_mapper.toml").read_text(encoding="utf-8"),
            )
            self.assertEqual(self.run_tool(home, "tiers", "check").returncode, 0)

    def test_unmanaged_tier_registry_collision_aborts_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "model-tiers.toml").write_text(
                '[tiers.T1]\nenabled = true\nmodel_provider = "openai"\nmodel = "other"\nsupported_efforts = ["max"]\n',
                encoding="utf-8",
            )
            before = (home / "model-tiers.toml").read_bytes()
            result = self.apply(home)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((home / "model-tiers.toml").read_bytes(), before)
            self.assertFalse((home / "config.toml").exists())

    def test_unmanaged_agent_collision_aborts_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            agent_dir = home / "agents"
            agent_dir.mkdir(parents=True)
            collision = agent_dir / "code_mapper.toml"
            collision.write_text('name = "someone_else"\n', encoding="utf-8")
            result = self.apply(home)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(collision.read_text(encoding="utf-8"), 'name = "someone_else"\n')
            self.assertFalse((home / "config.toml").exists())

    def test_provider_mismatch_fails_closed_without_rewriting_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.toml"
            config.write_text('model = "old"\nmodel_provider = "other-provider"\n', encoding="utf-8")
            before = config.read_bytes()
            result = self.apply(home)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse((home / "model-tiers.toml").exists())

    def test_invalid_toml_aborts_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.toml"
            config.write_text("[agents\n", encoding="utf-8")
            before = config.read_bytes()
            result = self.apply(home)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse((home / "agents").exists())

    def test_multiline_target_aborts_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.toml"
            config.write_text('model = [\n  "old-model",\n]\n', encoding="utf-8")
            before = config.read_bytes()
            result = self.apply(home)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse((home / "agents").exists())

    def test_backup_and_rollback_restore_exact_manifest_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.toml"
            original = '# keep\nmodel = "before"\n'
            config.write_text(original, encoding="utf-8")
            applied = self.apply(home)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            backup = self.backup_from(applied)
            config.write_text('model = "tampered"\n', encoding="utf-8")
            rolled = self.run_tool(home, "rollback", "--backup", backup)
            self.assertEqual(rolled.returncode, 0, rolled.stderr)
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertFalse((home / "AGENTS.md").exists())
            self.assertFalse((home / "model-tiers.toml").exists())
            manifest = json.loads((Path(backup) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], 2)
            self.assertEqual(manifest["codex_home"], str(home.resolve()))

    def test_non_empty_override_is_active_instruction_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            base = home / "AGENTS.md"
            override = home / "AGENTS.override.md"
            base.write_text("base instructions\n", encoding="utf-8")
            override.write_text("override instructions\n", encoding="utf-8")
            result = self.apply(home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(base.read_text(encoding="utf-8"), "base instructions\n")
            self.assertIn(
                "BEGIN MANAGED: configure-sol-luna-subagents",
                override.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
