"""SPINE registry validation and authenticated MCP tool integration tests."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vortex.server import create_server
from vortex.settings import Settings
from vortex.spine import advisory_task_goals, classify, survey_organs
from vortex.store import VortexStore


class SpineSurveyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "organs"
        self.root.mkdir()
        (self.root / "memory").mkdir()
        (self.root / "memory" / "ready.txt").write_text("ready", encoding="utf-8")
        self.registry = self.base / "registry.json"
        self.write_registry([{
            "id": "citadel", "role": "memory", "canon": "yes", "expected": "present",
            "path": str(self.root / "memory"), "sentinel": "ready.txt",
        }])
        self.env = patch.dict(os.environ, {
            "VORTEX_SPINE_REGISTRY": str(self.registry),
            "VORTEX_SPINE_ORGAN_ROOTS": str(self.root),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def write_registry(self, organs: object) -> None:
        self.registry.write_text(json.dumps({"organs": organs}), encoding="utf-8")

    def test_registry_byte_limit(self) -> None:
        self.registry.write_bytes(b" " * (256 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "size limit"):
            survey_organs()

    def test_survey_reports_status_without_local_paths(self) -> None:
        rows, digest = survey_organs()
        self.assertEqual(rows, [{"id": "citadel", "role": "memory", "status": "LIVE", "canon": "yes"}])
        self.assertEqual(len(digest), 64)
        self.assertNotIn(str(self.root), json.dumps(rows))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "not configured"):
                survey_organs()

    def test_missing_sentinel_is_degraded_and_generates_read_only_goal(self) -> None:
        self.write_registry([{
            "id": "citadel", "role": "memory", "canon": "yes", "expected": "present",
            "path": str(self.root / "memory"), "sentinel": "missing.txt",
        }])
        rows, _ = survey_organs()
        self.assertEqual(rows[0]["status"], "DEGRADED")
        tasks = advisory_task_goals(rows)
        self.assertEqual(len(tasks), 1)
        self.assertIn("read-only", tasks[0][1])
        self.assertIn("no repair authorized", tasks[0][1])

    def test_registry_argument_must_match_allowlisted_config(self) -> None:
        other = self.base / "other.json"
        other.write_text('{"organs":[]}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must match"):
            survey_organs(str(other))

    def test_rejects_traversal_symlink_secret_and_invalid_registry(self) -> None:
        cases = [
            ({"id": "../outside", "role": "memory", "canon": "yes", "expected": "present", "path": str(self.root / "memory")}, "identifiers"),
            ({"id": "secret", "role": "memory", "canon": "yes", "expected": "present", "path": str(self.base / ".env")}, "safe roots"),
        ]
        for organ, expected in cases:
            with self.subTest(expected=expected):
                self.write_registry([organ])
                with self.assertRaises(ValueError):
                    survey_organs()
        try:
            (self.root / "link").symlink_to(self.root / "memory", target_is_directory=True)
        except OSError:
            pass  # Windows without Developer Mode/admin cannot create symlinks.
        else:
            self.write_registry([{
                "id": "link", "role": "memory", "canon": "yes", "expected": "present",
                "path": str(self.root / "link"),
            }])
            with self.assertRaisesRegex(ValueError, "symbolic links or junctions"):
                survey_organs()
        self.write_registry([{
            "id": "bad", "role": "memory", "canon": "yes", "expected": "present",
            "path": str(self.root / "memory"), "sentinel": "../.env",
        }])
        with self.assertRaises(ValueError):
            survey_organs()
        self.write_registry([{
            "id": "token-store", "role": "memory", "canon": "yes", "expected": "present",
            "path": str(self.root / "memory"), "sentinel": "api-token.txt",
        }])
        with self.assertRaisesRegex(ValueError, "secret paths"):
            survey_organs()
        for invalid in (None, False, 1, [], {}):
            self.write_registry([{
                "id": "bad-canon", "role": "memory", "canon": invalid, "expected": "present",
                "path": str(self.root / "memory"),
            }])
            with self.assertRaises(ValueError):
                survey_organs()
        self.write_registry([{
            "id": "bad-expected", "role": "memory", "canon": "yes", "expected": False,
            "path": str(self.root / "memory"),
        }])
        with self.assertRaisesRegex(ValueError, "expected state"):
            survey_organs()
        self.write_registry([{
            "id": "bad-role", "role": "role\\nwith newline", "canon": "yes", "expected": "present",
            "path": str(self.root / "memory"),
        }])
        with self.assertRaisesRegex(ValueError, "role is invalid"):
            survey_organs()
        self.write_registry([])
        self.registry.write_text('{"organs":[],"extra":"no"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "only an organs list"):
            survey_organs()

    def test_bounded_lists_and_classification(self) -> None:
        self.write_registry([{
            "id": f"organ-{i}", "role": "worker", "expected": "present", "path": str(self.root / "memory")
        } for i in range(101)])
        with self.assertRaisesRegex(ValueError, "bounded list"):
            survey_organs()
        self.assertEqual(classify("absent", False, True, "no"), "ABSENT")
        self.assertEqual(classify("absent", True, True, "no"), "UNEXPECTED")
        with self.assertRaises(ValueError):
            classify("maybe", True, True, "no")


class SpineServerToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VortexStore(":memory:")
        settings = Settings(
            auth_mode="development", host="127.0.0.1", port=8000, data_dir=":memory:",
            development_tokens={"hermes-1": "a", "hermes-2": "b", "antigravity-1": "c", "spark-1": "d"},
        )
        self.server = create_server(self.store, settings)
        self.addCleanup(self.store.close)

    def invoke(self, tool_name: str, **arguments: object) -> object:
        tool = next(item for item in self.server._tool_manager.list_tools() if item.name == tool_name)
        access = type("Access", (), {"client_id": "hermes-1"})()
        with patch("vortex.server.get_access_token", return_value=access):
            return asyncio.run(self.server._tool_manager.call_tool(tool_name, arguments))

    def test_authenticated_tools_registered_and_survey_is_read_only(self) -> None:
        names = {tool.name for tool in self.server._tool_manager.list_tools()}
        self.assertTrue({"vortex_spine_survey", "vortex_spine_plan"}.issubset(names))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "organs"
            root.mkdir()
            (root / "citadel").mkdir()
            registry = Path(tmp) / "registry.json"
            registry.write_text(json.dumps({"organs": [{
                "id": "citadel", "role": "memory", "expected": "present", "canon": "yes",
                "path": str(root / "citadel"),
            }]}), encoding="utf-8")
            with patch.dict(os.environ, {
                "VORTEX_SPINE_REGISTRY": str(registry), "VORTEX_SPINE_ORGAN_ROOTS": str(root),
            }):
                result = self.invoke("vortex_spine_survey")
        self.assertEqual(result["organs"][0]["status"], "LIVE")
        self.assertEqual(self.store.list_tasks("hermes-1"), [])
        self.assertNotIn(str(root), json.dumps(result))

    def test_plan_creates_bounded_advisory_tasks_only_with_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "organs"
            (root / "citadel").mkdir(parents=True)
            registry = Path(tmp) / "registry.json"
            registry.write_text(json.dumps({"organs": [{
                "id": "citadel", "role": "memory", "expected": "present", "canon": "yes",
                "path": str(root / "citadel"), "sentinel": "missing.txt",
            }]}), encoding="utf-8")
            with patch.dict(os.environ, {
                "VORTEX_SPINE_REGISTRY": str(registry), "VORTEX_SPINE_ORGAN_ROOTS": str(root),
            }):
                no_opt_in = self.invoke("vortex_spine_plan")
                self.assertEqual(self.store.list_tasks("hermes-1"), [])
                planned = self.invoke("vortex_spine_plan", create_tasks=True)
                repeated = self.invoke("vortex_spine_plan", create_tasks=True)
        self.assertFalse(no_opt_in["tasks_created"])
        self.assertEqual(planned["tasks_created"], True)
        self.assertEqual(len(planned["tasks"]), 1)
        task = self.store.get_task("hermes-1", planned["tasks"][0]["task_id"])
        self.assertEqual(task["created_by"], "hermes-1")
        self.assertEqual(task["assigned_to"], "hermes-2")
        self.assertIn("read-only", task["goal"])
        self.assertIn("no repair authorized", task["goal"])
        self.assertEqual(len(self.store.task_events("hermes-1", task["task_id"])), 1)
        self.assertEqual(repeated["tasks"][0]["task_id"], task["task_id"])
        self.assertEqual(len(self.store.list_tasks("hermes-1")), 1)


if __name__ == "__main__":
    unittest.main()
