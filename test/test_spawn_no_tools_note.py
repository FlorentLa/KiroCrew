"""A sub-agent whose spec declares ``"tools": []`` is flagged to its caller.

On a backend that enforces the declaration such an agent cannot write a file,
run a command or make a commit, but it still answers a task that asks for one,
usually by narrating the steps. ``spawn_run`` says so at dispatch and
``spawn_sub_agents`` says so beside the result; on a backend that does not
enforce it nothing is said, since the agent may really have tools. The
post-fan-out synthesis prompt treats every completion as a claim to check on
disk rather than as proof.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.mcp_tools import spawn as spawn_tools

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_SPECS: dict[str, Any] = {
    "reasoner": {"name": "reasoner", "tools": []},
    "builder": {"name": "builder", "tools": ["fs_write", "execute_bash"]},
    "no-key": {"name": "no-key"},
    "odd": {"name": "odd", "tools": "none"},
}


def _snapshot(agent: str, *, work_dir: Any = None) -> Any:
    return _SPECS.get(agent)


@pytest.fixture
def specs():
    with (
        patch("kiro_crew.acp.session_mcp.agent_spec_snapshot", side_effect=_snapshot) as fake,
        patch.object(spawn_tools, "_backend_enforces_no_tools", return_value=True),
    ):
        yield fake


def _with_backend(backend: str):
    cfg = MagicMock()
    cfg.agent.acp_backend = backend
    return patch.object(spawn_tools.KiroCrewConfig, "load", return_value=cfg)


def _spawn_run(args: dict[str, Any]) -> str:
    from kiro_crew import mcp_core

    counter = iter(range(1, 100))

    def _fake_post(path: str, body: dict) -> dict:
        return {"id": f"a{next(counter)}"}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        return mcp_core._call_tool_inner("spawn_run", args)


class TestDeclaresNoTools:
    @pytest.mark.parametrize(
        ("agent", "expected"),
        [
            ("reasoner", True),
            ("builder", False),
            ("no-key", False),
            ("odd", False),
            ("missing", False),
            ("", False),
        ],
    )
    def test_only_an_explicit_empty_list_counts(self, specs, agent, expected):
        assert spawn_tools._declares_no_tools(agent) is expected

    def test_an_unreadable_spec_attaches_nothing(self):
        with patch(
            "kiro_crew.acp.session_mcp.agent_spec_snapshot", side_effect=RuntimeError("boom")
        ):
            assert spawn_tools._declares_no_tools("reasoner") is False

    def test_the_cwd_is_the_project_the_spec_is_resolved_against(self, specs):
        spawn_tools._declares_no_tools("reasoner", "/work/repo")
        assert specs.call_args.kwargs["work_dir"] == "/work/repo"

    def test_a_real_zero_tool_spec_on_disk_is_recognised(self, tmp_path, monkeypatch):
        from kiro_crew import agent as agent_mod

        (tmp_path / "reasoner.json").write_text(json.dumps(_SPECS["reasoner"]))
        (tmp_path / "builder.json").write_text(json.dumps(_SPECS["builder"]))
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path)
        with _with_backend(""):
            assert spawn_tools._declares_no_tools("reasoner") is True
            assert spawn_tools._declares_no_tools("builder") is False


class TestBackendEnforcement:
    @pytest.mark.parametrize(
        ("backend", "expected"),
        [("", True), ("kas", True), ("opencode", True), ("claude", False), ("codex", False)],
    )
    def test_the_note_needs_a_backend_that_enforces_the_ban(self, backend, expected):
        with (
            patch("kiro_crew.acp.session_mcp.agent_spec_snapshot", side_effect=_snapshot),
            _with_backend(backend),
        ):
            assert spawn_tools._declares_no_tools("reasoner") is expected

    def test_an_unreadable_config_attaches_nothing(self):
        with (
            patch("kiro_crew.acp.session_mcp.agent_spec_snapshot", side_effect=_snapshot),
            patch.object(spawn_tools.KiroCrewConfig, "load", side_effect=OSError("gone")),
        ):
            assert spawn_tools._declares_no_tools("reasoner") is False

    def test_spawn_run_on_a_non_enforcing_backend_gives_no_note(self):
        with (
            patch("kiro_crew.acp.session_mcp.agent_spec_snapshot", side_effect=_snapshot),
            _with_backend("codex"),
        ):
            out = _spawn_run({"task": "write a.py", "agent": "reasoner"})
        assert spawn_tools.NO_TOOLS_AGENT_NOTE not in out


class TestSpawnRunDispatchNote:
    def test_a_zero_tool_agent_is_flagged_once_per_name(self, specs):
        out = _spawn_run({"tasks": ["write a.py", "write b.py"], "agent": "reasoner"})
        notes = [line for line in out.splitlines() if spawn_tools.NO_TOOLS_AGENT_NOTE in line]
        assert notes == [f"⚠ Agent 'reasoner' {spawn_tools.NO_TOOLS_AGENT_NOTE}"]

    def test_an_agent_with_tools_gets_no_note(self, specs):
        out = _spawn_run({"task": "write a.py", "agent": "builder"})
        assert spawn_tools.NO_TOOLS_AGENT_NOTE not in out

    def test_the_default_agent_gets_no_note_and_no_spec_read(self, specs):
        out = _spawn_run({"task": "write a.py"})
        assert spawn_tools.NO_TOOLS_AGENT_NOTE not in out
        specs.assert_not_called()

    def test_a_mixed_wave_flags_only_the_zero_tool_member(self, specs):
        out = _spawn_run({"tasks": ["think", "build"], "agents": ["reasoner", "builder"]})
        assert f"⚠ Agent 'reasoner' {spawn_tools.NO_TOOLS_AGENT_NOTE}" in out
        assert "Agent 'builder'" not in out

    def test_the_dispatch_is_not_refused(self, specs):
        out = _spawn_run({"task": "write a.py", "agent": "reasoner"})
        assert out.startswith("Spawned 1 subagent(s).")


class TestSpawnSubAgentsResultNote:
    def _collect(self, agent: str) -> dict[str, Any]:
        from kiro_crew import mcp_core

        with (
            patch.object(mcp_core, "_post", return_value={"id": "a1"}),
            patch.object(
                mcp_core,
                "_get",
                return_value={"done": True, "agent": agent, "result": "wrote a.py, commit abc"},
            ),
            patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
            patch.object(mcp_core, "sel", MagicMock()),
        ):
            out = mcp_core._call_tool_inner(
                "spawn_sub_agents", {"agents": [{"agent_or_mode": agent, "prompt": "write a.py"}]}
            )
        return json.loads(out.split("\n\n")[0])

    def test_a_zero_tool_result_carries_the_unverified_field(self, specs):
        record = self._collect("reasoner")
        assert record["status"] == "completed"
        assert record["unverified"] == f"This agent {spawn_tools.NO_TOOLS_AGENT_NOTE}"

    def test_a_result_from_an_agent_with_tools_has_none(self, specs):
        assert "unverified" not in self._collect("builder")


class TestSynthesisPrompt:
    def test_the_prompt_treats_completions_as_claims_to_check_on_disk(self):
        from kiro_crew.dashboard.state import SUBAGENT_SYNTHESIS_PREFIX, SUBAGENT_SYNTHESIS_PROMPT

        assert SUBAGENT_SYNTHESIS_PROMPT.startswith(SUBAGENT_SYNTHESIS_PREFIX)
        assert "claim, not proof" in SUBAGENT_SYNTHESIS_PROMPT
        assert "git status" in SUBAGENT_SYNTHESIS_PROMPT
        assert "did not find" in SUBAGENT_SYNTHESIS_PROMPT
