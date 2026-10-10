"""Every generated agent-spec writer runs the one governed-write tail.

``allowedTools``, a server's ``autoApprove``, the KAS ``permissions`` block and the
global ``mcp.json`` merge are four ways a call reaches a tool without the PreToolUse
gate seeing it first. ``auto_approve.govern_spec`` closes all four, and
``auto_approve.write_governed_spec`` runs it and writes the file. These tests pin
three things:

* structurally, no writer in ``agent_materialization`` reaches the write primitive
  any other way, so a new installer cannot omit a pass;
* behaviourally, every installer governs an operator override that tries each of
  the four channels;
* the tail itself: its KAS policies, its refusals and its idempotence.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from kiro_crew import agent, agent_state
from kiro_crew.agent_materialization import (
    auto_approve,
    conductor_agents,
    fork_refresh,
    service_agents,
    worker_agent,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

_PACKAGE = Path(auto_approve.__file__).resolve().parent

#: The one module allowed to call the write primitive directly. It commits the
#: PRIMARY spec, which ``rebuild_agent_config`` governs through its own passes
#: (seeded, never refreshed, KAS block; the final ceiling pass; the
#: ``_refresh_dynamic_fields`` ``includeMcpJson`` pin) rather than a derived one.
_PRIMARY_SPEC_WRITER = "default_spec_commit.py"


@pytest.fixture(autouse=True)
def _accepting_kiro_cli(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin a kiro-cli that accepts ``permissions``, so the derive is observable."""
    _floor_monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )


def _direct_write_calls(source: str) -> list[tuple[str, int]]:
    """``(enclosing function, line)`` for every call to ``_atomic_json_write``."""
    tree = ast.parse(source)
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.Call):
                func = child.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if called == "_atomic_json_write":
                    found.append((owner, child.lineno))
            visit(child, name)

    visit(tree, "<module>")
    return found


def test_no_spec_writer_bypasses_the_governed_tail() -> None:
    """The only direct write in the package is the tail's own, plus the primary spec."""
    offenders: list[str] = []
    for path in sorted(_PACKAGE.glob("*.py")):
        if path.name == _PRIMARY_SPEC_WRITER:
            continue
        for owner, line in _direct_write_calls(path.read_text(encoding="utf-8")):
            if path.name == "auto_approve.py" and owner == "write_governed_spec":
                continue
            offenders.append(f"{path.name}:{line} in {owner}")
    assert offenders == [], (
        "write a generated agent spec through auto_approve.write_governed_spec, "
        f"not agent_mod._atomic_json_write: {offenders}"
    )


def test_the_structural_scan_sees_a_direct_write() -> None:
    """Control: the scan above is not vacuous."""
    sample = "def _install_x():\n    agent_mod._atomic_json_write(path, config)\n"
    assert _direct_write_calls(sample) == [("_install_x", 2)]


def _derive(config: dict[str, Any], filename: str = "x.json") -> Any:
    from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions

    return derived_agent_permissions(config.get("allowedTools") or [], filename)


# -- every installer governs an operator override ---------------------------------

#: An entry whose transport no managed spec declares, so it declares no verb, and
#: the strip must remove every ``autoApprove`` it carries.
_UNDECLARED_CORE = {"command": "/operator/elsewhere", "args": [], "autoApprove": ["session_stop"]}
_STALE_PERMISSIONS = {"rules": [{"capability": "execute_bash", "effect": "allow"}]}


def _overridden_template() -> dict[str, Any]:
    """What ``build_agent_config`` returns when ``agent.json`` tries every channel."""
    return {
        "name": "kirocrew",
        "prompt": "x",
        "tools": ["@kirocrew-core"],
        "allowedTools": ["@kirocrew-core"],
        "mcpServers": {"kirocrew-core": dict(_UNDECLARED_CORE)},
        "includeMcpJson": True,
        "useLegacyMcpJson": True,
        "permissions": _STALE_PERMISSIONS,
    }


_INSTALLERS: dict[str, Callable[[], object]] = {
    "kirocrew-conductor.json": conductor_agents._install_conductor_agent,
    "kirocrew-ledger-conductor.json": conductor_agents._install_ledger_conductor_agent,
    "kirocrew-pipeline-conductor.json": conductor_agents._install_pipeline_conductor_agent,
    "kirocrew-security-conductor.json": conductor_agents._install_security_conductor_agent,
    "kirocrew-worker.json": worker_agent._install_worker_agent,
    "kirocrew-research.json": service_agents._install_research_agent,
    "kirocrew-dashboard-manager.json": service_agents._install_dashboard_manager_agent,
    "kirocrew-lite.json": service_agents._install_lite_agent_fallback,
    "kirocrew-guest.json": service_agents._install_guest_agent,
    "kirocrew-knowledge.json": service_agents._install_knowledge_agent,
}


#: The service specs that mount no tool and no server, so they carry no KAS block.
_MOUNTS_NOTHING = {"kirocrew-lite.json", "kirocrew-guest.json", "kirocrew-knowledge.json"}


@pytest.fixture
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent, "build_agent_config", lambda *a, **k: _overridden_template())
    monkeypatch.setattr(agent, "_kirocrew_mcp_invocation", lambda sub: ("/resolved/kc", [sub]))
    monkeypatch.setattr(worker_agent, "_installed_default_spec", lambda: None)
    return tmp_path


@pytest.mark.parametrize("filename", sorted(_INSTALLERS))
def test_every_installer_pins_the_global_mcp_json_merge_off(
    agents_dir: Path, filename: str
) -> None:
    _INSTALLERS[filename]()
    spec = json.loads((agents_dir / filename).read_text(encoding="utf-8"))
    assert spec["includeMcpJson"] is False
    assert "useLegacyMcpJson" not in spec


@pytest.mark.parametrize("filename", sorted(_INSTALLERS))
def test_every_installer_strips_an_ungoverned_auto_approve(
    agents_dir: Path, filename: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the strict floor, an ``autoApprove`` no spec declares does not survive.

    An ungoverned host honours an owner-written ``autoApprove`` by default, so the
    floor is pinned strict here: what is under test is that every writer reaches the
    strip at all, not the owner-honouring rule the strip applies.
    """
    strict = auto_approve.strip_ungoverned_auto_approve
    monkeypatch.setattr(
        auto_approve,
        "strip_ungoverned_auto_approve",
        lambda servers: strict(servers, honour_owner_written=False),
    )
    _INSTALLERS[filename]()
    spec = json.loads((agents_dir / filename).read_text(encoding="utf-8"))
    for name, entry in (spec.get("mcpServers") or {}).items():
        assert "session_stop" not in (entry.get("autoApprove") or []), name


@pytest.mark.parametrize(
    "filename",
    sorted(set(_INSTALLERS) - _MOUNTS_NOTHING),
)
def test_every_granting_installer_derives_its_kas_block(agents_dir: Path, filename: str) -> None:
    """An inherited ``permissions`` block is replaced by the derivation, never kept."""
    _INSTALLERS[filename]()
    spec = json.loads((agents_dir / filename).read_text(encoding="utf-8"))
    assert spec["permissions"] != _STALE_PERMISSIONS
    assert spec["permissions"] == _derive(spec, filename)


# -- the tail itself ---------------------------------------------------------------


def test_derive_replaces_an_inherited_block() -> None:
    config = {"tools": ["fs_read"], "allowedTools": ["fs_read"], "permissions": _STALE_PERMISSIONS}
    auto_approve.govern_spec(config, agent_filename="x.json", source="t")
    assert config["permissions"] == _derive(config)


def test_seed_never_edits_a_block_that_is_present() -> None:
    config = {"tools": ["fs_read"], "allowedTools": ["fs_read"], "permissions": _STALE_PERMISSIONS}
    auto_approve.govern_spec(config, agent_filename="x.json", source="t", kas_policy="seed")
    assert config["permissions"] == _STALE_PERMISSIONS


def test_seed_derives_a_block_that_is_absent() -> None:
    config = {"tools": ["fs_read"], "allowedTools": ["fs_read"]}
    auto_approve.govern_spec(config, agent_filename="x.json", source="t", kas_policy="seed")
    assert config["permissions"] == _derive(config)


def test_none_writes_no_block_for_a_spec_that_mounts_nothing() -> None:
    config: dict[str, Any] = {"tools": [], "mcpServers": {}}
    auto_approve.govern_spec(config, agent_filename="x.json", source="t", kas_policy="none")
    assert "permissions" not in config
    assert config["includeMcpJson"] is False


@pytest.mark.parametrize(
    "config",
    [{"tools": ["fs_read"]}, {"allowedTools": ["fs_read"]}, {"mcpServers": {"s": {}}}],
)
def test_none_is_refused_for_a_spec_that_mounts_or_grants(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="mounts nothing"):
        auto_approve.govern_spec(config, agent_filename="x.json", source="t", kas_policy="none")


def test_an_unknown_policy_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown kas_policy"):
        auto_approve.govern_spec({}, agent_filename="x.json", source="t", kas_policy="skip")


def test_the_ceiling_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto_approve, "_may_auto_approve", lambda ref: ref != "fs_read")
    config = {"tools": ["fs_read", "grep"], "allowedTools": ["fs_read", "grep"]}
    auto_approve.govern_spec(config, agent_filename="x.json", source="t")
    assert config["allowedTools"] == ["grep"]
    assert config["tools"] == ["fs_read", "grep"]


def test_the_tail_is_idempotent() -> None:
    config = _overridden_template()
    auto_approve.govern_spec(config, agent_filename="x.json", source="t")
    once = json.loads(json.dumps(config))
    auto_approve.govern_spec(config, agent_filename="x.json", source="t")
    assert config == once


# -- the fork refresh is a caller too ---------------------------------------------


def test_a_fork_of_a_custom_template_has_the_merge_pinned_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No plumbing refresh runs for this fork, so the tail is what pins the merge."""
    monkeypatch.setattr(agent, "_fork_refresh_failed", frozenset())
    monkeypatch.setattr(
        agent_state,
        "all_fork_info",
        lambda: {"crewfork": {"private_to": "crew", "forked_from": "my-template"}},
    )
    monkeypatch.setattr(agent_state, "get_capabilities", lambda _name: None)
    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: SimpleNamespace(agents={"crew": SimpleNamespace(kiro_agent="crewfork")})
        ),
    )
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = tmp_path / "crewfork.json"
    kept = {"rules": [{"capability": "fs_read", "effect": "allow"}]}
    spec.write_text(
        json.dumps(
            {
                "name": "crewfork",
                "tools": [],
                "allowedTools": [],
                "includeMcpJson": True,
                "useLegacyMcpJson": True,
                "permissions": kept,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: spec)
    fork_refresh._refresh_forked_templates_locked(gated_off=frozenset())
    written = json.loads(spec.read_text(encoding="utf-8"))
    assert written["includeMcpJson"] is False
    assert "useLegacyMcpJson" not in written
    # A fork is the user's file: its KAS block is seeded when absent, never edited.
    assert written["permissions"] == kept
    assert agent._fork_refresh_failed == frozenset()
