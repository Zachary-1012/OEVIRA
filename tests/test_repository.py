"""Negative cases for repository admission; not product acceptance."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_repository", ROOT / "scripts/verify_repository.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class RepositoryAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="oevira-repository-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in validator.DIRECTORIES | validator.ROOT_FILES:
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.is_file():
                shutil.copy2(source, self.root / name)
        self.tracked_paths = [
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and not any(part in validator.IGNORED for part in path.relative_to(self.root).parts)
        ]

    def validate(self):
        return validator.validate(self.root, tracked_paths=self.tracked_paths)

    def test_approved_structure_is_not_a_release(self):
        self.assertEqual(self.validate(), [])
        self.assertTrue(any("PRODUCT_NOT_IMPLEMENTED" in error for error in validator.validate(self.root, require_product=True, tracked_paths=self.tracked_paths)))

    def test_environment_commands_do_not_change_the_specification_stage(self):
        for relative in ("scripts/setup.ps1", "scripts/start.ps1", "scripts/stop.ps1", "scripts/check_python_runtime.py"):
            self.assertTrue((self.root / relative).is_file(), relative)
        self.assertEqual(self.validate(), [])
        path = self.root / ".oevira/repository.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["stage"] = "development"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertTrue(any("own blocking checks" in error for error in self.validate()))

    def test_missing_specification_blocks(self):
        (self.root / "docs/PRODUCT-SPEC.md").unlink()
        self.assertTrue(any("Required specification" in error for error in self.validate()))

    def test_product_code_cannot_hide_in_specification_gate(self):
        (self.root / "server/app.py").write_text("print('not accepted')\n", encoding="utf-8")
        self.assertTrue(any("implementation cannot enter" in error for error in self.validate()))

    def test_product_code_cannot_hide_in_docs_scripts_or_tests(self):
        cases = (
            ("docs/hidden.py", "print('hidden')\n", "hidden in docs"),
            ("scripts/helper.py", "print('helper')\n", "Non-specification file in scripts"),
            ("scripts/unapproved.ps1", "Write-Output 'helper'\n", "Non-specification file in scripts"),
            ("scripts/setup/extra.ps1", "Write-Output 'helper'\n", "Non-specification file in scripts"),
            ("tests/product_hook.py", "print('hook')\n", "Non-specification file in tests"),
        )
        for relative, source, expected in cases:
            with self.subTest(relative=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source, encoding="utf-8")
                self.assertTrue(any(expected in error for error in self.validate()))
                path.unlink()

    def test_private_data_blocks(self):
        (self.root / "docs/runtime.private.json").write_text("{}", encoding="utf-8")
        self.assertTrue(any("Private data" in error for error in self.validate()))

    def test_manifest_cannot_weaken_fixed_source_hash_or_check_sets(self):
        path = self.root / ".oevira/repository.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for field, mutate, expected in (
            ("source_roots", lambda value: value.clear(), "Source responsibilities"),
            ("preserved_sha256", lambda value: value.pop("LICENSE"), "digest set"),
            ("required_checks", lambda value: value.pop(), "Required workflow checks"),
        ):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(original))
                mutate(changed[field])
                path.write_text(json.dumps(changed), encoding="utf-8")
                self.assertTrue(any(expected in error for error in self.validate()))
        path.write_text(json.dumps(original), encoding="utf-8")

    def test_workflow_main_triggers_names_and_steps_are_required(self):
        path = self.root / ".github/workflows/repository-checks.yml"
        original = path.read_text(encoding="utf-8")
        changes = (
            ("  push:\n    branches: [main]", "  push:\n    branches: [develop]", "push checks for main"),
            ("  pull_request:\n    branches: [main]", "  pull_request:\n    branches: [develop]", "pull_request checks for main"),
            ("    name: Repository contracts", "    name: Renamed contracts", "check name"),
            ("        run: python -m unittest discover -s tests -v\n", "", "missing required step"),
        )
        for old, new, expected in changes:
            with self.subTest(expected=expected):
                self.assertIn(old, original)
                path.write_text(original.replace(old, new, 1), encoding="utf-8")
                self.assertTrue(any(expected in error for error in self.validate()))
        path.write_text(original, encoding="utf-8")

    def test_forced_tracked_runtime_env_cookie_session_and_private_key_paths_block(self):
        cases = (".runtime/state.txt", "docs/.env.local", "docs/cookies.json", "docs/browser_session.json", "docs/private_key.pem", "docs/runtime.db")
        for relative in cases:
            with self.subTest(relative=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("temporary test value", encoding="utf-8")
                self.tracked_paths.append(relative)
                self.assertTrue(any("Sensitive or ignored path is tracked" in error for error in self.validate()))
                self.tracked_paths.remove(relative)
                path.unlink()

    def test_untracked_local_runtime_is_not_inspected(self):
        local = self.root / ".runtime/local.txt"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("ghp_" + "A" * 36, encoding="utf-8")
        errors = self.validate()
        self.assertFalse(any("Credential-like content" in error for error in errors))
        self.assertFalse(any(".runtime" in error for error in errors))

    def test_credential_scan_covers_tracked_text_extensions(self):
        path = self.root / "docs/report.csv"
        path.write_text("status,token\nactive,ghp_" + "A" * 36 + "\n", encoding="utf-8")
        self.tracked_paths.append("docs/report.csv")
        self.assertTrue(any("Credential-like content found: docs/report.csv" in error for error in self.validate()))

    def test_historical_attribution_is_immutable(self):
        (self.root / "docs/reference/NOTICE").write_text("changed", encoding="utf-8")
        self.assertTrue(any("attribution bytes changed" in error for error in self.validate()))

    def test_broken_active_link_blocks(self):
        with (self.root / "README.md").open("a", encoding="utf-8") as target:
            target.write("\n[missing](missing-document.md)\n")
        self.assertTrue(any("Broken local link" in error for error in self.validate()))

    def test_release_flag_is_not_an_acceptance_shortcut(self):
        path = self.root / ".oevira/repository.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["release_ready"] = True
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertTrue(any("own blocking checks" in error for error in self.validate()))


    def test_active_link_to_existing_external_file_blocks(self):
        # A historical-root link once passed on the developer's machine because
        # the old checkout existed, then broke in an independent checkout.
        with tempfile.TemporaryDirectory(prefix="oevira-historical-source-", dir=self.root.parent) as outside:
            original = Path(outside) / "original.md"
            original.write_text("preserved original", encoding="utf-8")
            target = f"../{Path(outside).name}/original.md"
            self.assertTrue((self.root / target).is_file())
            with (self.root / "README.md").open("a", encoding="utf-8") as document:
                document.write(f"\n[historical source]({target})\n")
            self.assertTrue(any("Active document link leaves repository" in error for error in self.validate()))

class ContinuityContextTests(unittest.TestCase):
    def test_latest_authorization_is_not_replaced_by_a_stale_wait_gate(self):
        spec = importlib.util.spec_from_file_location(
            "oevira_continuity_contract_test", ROOT / ".codex/continuity.py")
        continuity = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(continuity)
        # Keep the test within this checkout; never resolve the user's old root.
        continuity.FORMER_ROOT = ROOT / "inactive-context-test"
        for event in ("SessionStart", "UserPromptSubmit"):
            result = continuity.context_for({"hook_event_name": event, "cwd": str(ROOT)})
            context = result["hookSpecificOutput"]["additionalContext"]
            self.assertIn("latest direct user authorization", context)
            self.assertNotIn("report-then-wait", context)
            self.assertIn("task-scoped development", context)
            self.assertIn("nor resumes a paused task", context)
            self.assertIn("does not modify user configuration", context)
            self.assertNotIn("subagent model choice", context)
        self.assertEqual({}, continuity.context_for(None))
        self.assertEqual({}, continuity.context_for({"hook_event_name": "Stop", "cwd": str(ROOT)}))
        self.assertEqual({}, continuity.context_for({"hook_event_name": "SessionStart", "cwd": "relative"}))
        self.assertEqual({}, continuity.context_for({
            "hook_event_name": "SessionStart", "cwd": str(ROOT.parent / "unrelated-context-test")}))


if __name__ == "__main__":
    unittest.main()
