from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from crewhall.project_agents import project_agent_catalog
from crewhall.spawn_policy import AgentSpawnPolicy


def _project(root: Path, *, flags: dict | None = None) -> None:
    (root / ".git").mkdir(exist_ok=True)
    agent_dir = root / ".claude" / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "reviewer.md").write_text(
        "---\nname: api-reviewer\ndescription: Reviews API changes\n---\nReview APIs.\n",
        encoding="utf-8",
    )
    profile = {
        "provider": "claude",
        "model": "sonnet",
        "agent": "api-reviewer",
        "flags": flags or {"--max-turns": 5},
    }
    (root / "cwh-agents.json").write_text(
        json.dumps({"version": 1, "profiles": {"review": profile}}),
        encoding="utf-8",
    )


class ProjectAgentCatalog(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="spawn-catalog-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        _project(self.root)

    def test_catalog_pins_manifest_and_native_definition_content(self) -> None:
        first = project_agent_catalog(str(self.root))
        self.assertIsNone(first["error"])
        self.assertEqual(first["profiles"][0]["agent"], "api-reviewer")
        original = first["sha256"]

        definition = self.root / ".claude" / "agents" / "reviewer.md"
        definition.write_text(definition.read_text() + "\nNew behavior.\n", encoding="utf-8")
        second = project_agent_catalog(str(self.root))
        self.assertNotEqual(original, second["sha256"])

    def test_unknown_or_dangerous_flags_make_catalog_invalid(self) -> None:
        _project(self.root, flags={"--dangerously-skip-permissions": True})
        catalog = project_agent_catalog(str(self.root))
        self.assertIn("unsupported flags", catalog["error"])

    def test_symlink_manifest_is_rejected(self) -> None:
        manifest = self.root / "cwh-agents.json"
        target = self.root / "outside.json"
        target.write_text(manifest.read_text(), encoding="utf-8")
        manifest.unlink()
        manifest.symlink_to(target)
        catalog = project_agent_catalog(str(self.root))
        self.assertIn("regular file", catalog["error"])


class AgentSpawnPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="spawn-policy-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        _project(self.root)
        catalog = project_agent_catalog(str(self.root))
        self.assertIsNone(catalog["error"])
        self.team = SimpleNamespace(
            team_id="team-1", name="Team 1", spawner_id="spawner",
            agent_ids=["spawner"], workspace=str(self.root), host=None,
            workspace_mode="shared", spawner_catalog_sha256=catalog["sha256"],
        )
        self.creator = SimpleNamespace(
            agent_id="spawner", name="Spawner",
            session=SimpleNamespace(
                backend=SimpleNamespace(name="tmux"),
                spec=SimpleNamespace(cwd=str(self.root)),
            ),
        )
        self.agents = _AgentRepo([self.creator])
        self.controller = SimpleNamespace(
            teams=_TeamRepo(self.team), agents=self.agents,
            _agent_creators={}, _agent_creation_events={},
        )
        self.policy = AgentSpawnPolicy()

    def test_create_uses_approved_profile_and_ignores_execution_overrides(self) -> None:
        request = {
            "profile": "review", "name": "Worker",
            "kind": "opencode", "args": ["--yolo"],
            "backend": "ssh-tmux", "cwd": "/", "team": "team-1",
            "host": "remote", "workspace_mode": "worktree",
        }
        with mock.patch("crewhall.settings.provider_args", return_value=[]):
            approved = self.policy.create_request(self.controller, self.creator, request)

        self.assertEqual(approved["kind"], "claude")
        self.assertEqual(approved["backend"], "tmux")
        self.assertEqual(approved["cwd"], str(self.root))
        self.assertEqual(approved["team"], "team-1")
        self.assertEqual(approved["workspace_mode"], "shared")
        self.assertEqual(approved["host"], None)
        self.assertEqual(approved["args"], ["--model", "sonnet", "--agent", "api-reviewer",
                                           "--max-turns", "5"])

    def test_unapproved_or_changed_catalog_fails_closed(self) -> None:
        original = self.team.spawner_catalog_sha256
        self.team.spawner_catalog_sha256 = None
        with self.assertRaisesRegex(ValueError, "no operator-approved"):
            self.policy.create_request(self.controller, self.creator,
                                       {"profile": "review", "name": "Worker"})
        self.team.spawner_catalog_sha256 = original
        manifest = self.root / "cwh-agents.json"
        manifest.write_text(manifest.read_text() + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed after approval"):
            self.policy.create_request(self.controller, self.creator,
                                       {"profile": "review", "name": "Worker"})

    def test_cross_team_request_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "own team"):
            self.policy.create_request(
                self.controller, self.creator,
                {"profile": "review", "name": "Worker", "team": "other"},
            )

    def test_live_cap_counts_unknown_status_as_live(self) -> None:
        alive = _Harness("child-1", True)
        unknown = _Harness("child-2", None)
        self.agents.items.extend([alive, unknown])
        self.controller._agent_creators.update({"child-1": "spawner", "child-2": "spawner"})
        with mock.patch("crewhall.settings.get", return_value=2):
            self.assertEqual(self.policy.live_children(self.controller, "spawner"), 2)
            with self.assertRaisesRegex(ValueError, "max_live"):
                self.policy.check_create_slot(self.controller, "spawner")

    def test_daily_limit_reserves_and_persists_before_launch(self) -> None:
        now = time.time()
        self.controller._agent_creation_events = {"spawner": [now - 50000]}
        store = _Store()
        self.controller._store = store

        def persist() -> None:
            store.data = {"agent_creation_events": self.controller._agent_creation_events.copy()}

        self.controller._persist = persist
        with mock.patch("crewhall.settings.get", return_value=2):
            self.policy.reserve_create(self.controller, "spawner", now=now)
            self.assertEqual(len(store.load()["agent_creation_events"]["spawner"]), 2)
            with self.assertRaisesRegex(ValueError, "max_created_per_day"):
                self.policy.reserve_create(self.controller, "spawner", now=now + 1)

    def test_spawner_can_stop_only_its_own_child(self) -> None:
        child = _Harness("child", True)
        other = _Harness("other", True)
        self.agents.items.extend([child, other])
        self.team.agent_ids.extend(["child", "other"])
        self.controller._agent_creators = {"child": "spawner", "other": "different"}
        self.assertIs(self.policy.authorize_stop(self.controller, self.creator, "child"), child)
        with self.assertRaisesRegex(ValueError, "only agents it created"):
            self.policy.authorize_stop(self.controller, self.creator, "other")


class _TeamRepo:
    def __init__(self, team) -> None:
        self.team = team

    def list(self):
        return [self.team]


class _AgentRepo:
    def __init__(self, items) -> None:
        self.items = items

    def all(self):
        return self.items

    def resolve(self, agent_id):
        return next(item for item in self.items if item.agent_id == agent_id)


class _Harness:
    def __init__(self, agent_id: str, alive: bool | None) -> None:
        self.agent_id = agent_id
        self.name = agent_id
        status = _Status(alive)
        self.session = SimpleNamespace(status=status)


class _Status:
    def __init__(self, alive: bool | None) -> None:
        self._alive = alive

    @property
    def alive(self):
        if self._alive is None:
            raise RuntimeError("unknown status")
        return self._alive


class _Store:
    def __init__(self) -> None:
        self.data = {}

    def load(self):
        return self.data


if __name__ == "__main__":
    unittest.main()
