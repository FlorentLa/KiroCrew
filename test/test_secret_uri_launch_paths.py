"""A ``secret://`` env value is resolved in ONE place, and every other launch refuses.

The gateway's backend spawn (``mcp_gateway/secret_uri.resolve_secret_uris``) is the
only code that reads the vault for an MCP server. Every other path that starts a
server -- the session launching an unrouted server from the rebuilt agent spec, a
routed server the rewriter left unwrapped because a shared backend would withhold
part of its env, the stub's direct-exec fallback -- runs where the vault cannot be
read, so a server started there would get the literal reference as its credential:
it starts and every authenticated call fails. These tests pin that each of them
either reaches the gateway's spawn or refuses to start the server.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from kiro_crew.agent_materialization import mcp_sources
from kiro_crew.mcp_gateway import stub as stub_mod
from kiro_crew.mcp_gateway.hashing import expand_stub_flags
from kiro_crew.mcp_gateway.rewriter import (
    _WRAPPER_MARKER,
    _injectable_settings_servers,
    _rewrite_single_spec,
)
from kiro_crew.mcp_gateway.secret_uri import secret_reference_keys

REF = "secret://ATLASSIAN_TOKEN"


# --- the shared predicate ---------------------------------------------------


def test_secret_reference_keys_names_keys_only() -> None:
    env = {"B_TOKEN": REF, "A_TOKEN": "secret://other", "URL": "https://x", "N": 3}
    assert secret_reference_keys(env) == ["A_TOKEN", "B_TOKEN"]


@pytest.mark.parametrize("env", [None, [], "secret://x", {"K": "secret:/x"}, {}])
def test_secret_reference_keys_is_empty_for_anything_else(env: object) -> None:
    assert secret_reference_keys(env) == []


# --- rebuild projection: an unrouted server is withheld ----------------------


def _project(
    monkeypatch: pytest.MonkeyPatch, env: dict, routed: frozenset[str]
) -> tuple[dict, mcp_sources.ResolvedServers]:
    spec = {"command": "atlassian-mcp", "env": env}
    config = {"mcpServers": {"atlassian": dict(spec)}}
    sources = mcp_sources.McpSources(
        kirocrew={"atlassian": spec}, kiro_global={}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", lambda: routed)
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)
    resolved = mcp_sources.resolve_mcp_servers(
        config, sources, lambda cmd, _env: (f"/opt/bin/{cmd}", "")
    )
    return config, resolved


def test_an_unrouted_server_with_a_secret_reference_is_not_emitted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        config, resolved = _project(monkeypatch, {"API_TOKEN": REF}, frozenset())
    entry = config["mcpServers"]["atlassian"]
    # Kept muted, not dropped, so spec-only fields survive until it is routed.
    assert entry["disabled"] is True
    assert entry[mcp_sources.WITHHELD_KEY] is True
    assert "atlassian" not in resolved.unresolved
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "'API_TOKEN'" in messages and "MCP Management" in messages
    assert "ATLASSIAN_TOKEN" not in messages  # the secret's name is never echoed


def test_a_withheld_scope_server_keeps_its_spec_only_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field only the agent spec carries (here ``disabledTools``) is not lost
    while the server is withheld, and the mute lifts once it is routed."""
    scope = {"command": "atlassian-mcp", "env": {"API_TOKEN": REF}}
    config = {"mcpServers": {"atlassian": {**scope, "disabledTools": ["delete_page"]}}}
    sources = mcp_sources.McpSources(
        kirocrew={"atlassian": scope}, kiro_global={}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", frozenset)
    mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _env: (f"/opt/bin/{cmd}", ""))
    muted = config["mcpServers"]["atlassian"]
    assert muted["disabled"] is True
    assert muted["disabledTools"] == ["delete_page"]

    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", lambda: frozenset({"atlassian"}))
    mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _env: (cmd, ""))
    lifted = config["mcpServers"]["atlassian"]
    assert "disabled" not in lifted and mcp_sources.WITHHELD_KEY not in lifted
    assert lifted["disabledTools"] == ["delete_page"]


def test_the_shared_ref_sync_keeps_a_withheld_server_muted() -> None:
    """The mount pass must not re-enable an entry the rebuild withheld."""
    scope = {"command": "/opt/bin/atlassian-mcp", "env": {"API_TOKEN": REF}}
    config = {
        "mcpServers": {"atlassian": {**scope, "disabled": True, mcp_sources.WITHHELD_KEY: True}},
        "tools": ["@atlassian"],
        "allowedTools": ["@atlassian"],
    }
    sources = mcp_sources.McpSources(
        kirocrew={"atlassian": scope}, kiro_global={}, provider_global={}, managed_names=set()
    )
    mcp_sources.sync_shared_server_refs(config, sources, {"atlassian": "atlassian"})
    assert config["mcpServers"]["atlassian"]["disabled"] is True
    assert "@atlassian" not in config["allowedTools"]


def test_routing_on_a_host_without_a_gateway_routes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    import kiro_crew.config.loader as loader
    import kiro_crew.mcp_gateway as gw

    routed_cfg = types.SimpleNamespace(
        mcp_gateway=types.SimpleNamespace(stub_servers=["atlassian"])
    )
    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: routed_cfg))
    assert mcp_sources._gateway_routed_names() == frozenset({"atlassian"})
    monkeypatch.setattr(gw, "is_gateway_supported", lambda: False)
    assert mcp_sources._gateway_routed_names() == frozenset()


def test_a_routed_server_with_a_secret_reference_is_emitted_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, resolved = _project(monkeypatch, {"API_TOKEN": REF}, frozenset({"atlassian"}))
    entry = config["mcpServers"]["atlassian"]
    assert entry["env"]["API_TOKEN"] == REF  # resolved later, by the gateway
    assert "atlassian" not in resolved.unresolved


def _project_one(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    in_scope: bool,
    routed: frozenset[str] = frozenset(),
    app_owned: dict[str, bool] | None = None,
) -> tuple[dict, mcp_sources.ResolvedServers]:
    spec = {"command": "some-mcp", "env": {"API_TOKEN": REF}}
    config = {"mcpServers": {name: dict(spec)}}
    sources = mcp_sources.McpSources(
        kirocrew={name: spec} if in_scope else {},
        kiro_global={},
        provider_global={},
        managed_names=set(),
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", lambda: routed)
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", lambda: dict(app_owned or {}))
    resolved = mcp_sources.resolve_mcp_servers(
        config, sources, lambda cmd, _env: (f"/opt/bin/{cmd}", "")
    )
    return config, resolved


def test_a_server_routed_under_its_raw_name_is_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routing may be recorded under the raw slashed spelling or the alias."""
    config, resolved = _project_one(
        monkeypatch, "npm:@acme/mcp", in_scope=True, routed=frozenset({"npm:@acme/mcp"})
    )
    assert "npm:@acme/mcp" in config["mcpServers"]
    assert not resolved.unresolved


def test_a_rebuilt_alias_key_of_a_raw_routed_name_stays_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the first pass the spec key is the alias, while routing still lists
    the raw slashed name: the alias key must count as routed, not be muted."""
    config, _ = _project_one(
        monkeypatch, "acme-mcp", in_scope=False, routed=frozenset({"npm:@acme/mcp"})
    )
    entry = config["mcpServers"]["acme-mcp"]
    assert "disabled" not in entry and mcp_sources.WITHHELD_KEY not in entry


def test_a_server_only_in_the_agent_spec_is_kept_but_muted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent spec is the only copy of such an entry, so it is muted in place
    rather than dropped, and the mute lifts on the pass after it is routed."""
    config, resolved = _project_one(monkeypatch, "atlassian", in_scope=False)
    entry = config["mcpServers"]["atlassian"]
    assert entry["disabled"] is True
    assert entry[mcp_sources.WITHHELD_KEY] is True
    assert entry["env"]["API_TOKEN"] == REF  # the declaration survives intact
    assert "atlassian" not in resolved.unresolved

    sources = mcp_sources.McpSources(
        kirocrew={}, kiro_global={}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", lambda: frozenset({"atlassian"}))
    mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _env: (cmd, ""))
    lifted = config["mcpServers"]["atlassian"]
    assert "disabled" not in lifted
    assert mcp_sources.WITHHELD_KEY not in lifted


def test_a_user_written_mute_is_never_lifted(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = {"command": "some-mcp", "env": {"API_TOKEN": REF}, "disabled": True}
    config = {"mcpServers": {"atlassian": dict(spec)}}
    sources = mcp_sources.McpSources(
        kirocrew={}, kiro_global={}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", lambda: frozenset({"atlassian"}))
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)
    mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _env: (f"/opt/bin/{cmd}", ""))
    assert config["mcpServers"]["atlassian"]["disabled"] is True


@pytest.mark.parametrize("flag", [None, 0, "true"])
def test_a_non_boolean_user_mute_never_gains_the_withheld_record(
    monkeypatch: pytest.MonkeyPatch, flag: object
) -> None:
    """A mute the user wrote in any form stays the user's: the rebuild must not
    claim it, or a later pass would lift it and start the server."""
    spec = {"command": "some-mcp", "env": {"API_TOKEN": REF}, "disabled": flag}
    config = {"mcpServers": {"atlassian": dict(spec)}}
    sources = mcp_sources.McpSources(
        kirocrew={}, kiro_global={}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", frozenset)
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", dict)
    mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _env: (f"/opt/bin/{cmd}", ""))
    assert mcp_sources.WITHHELD_KEY not in config["mcpServers"]["atlassian"]


def test_an_app_registered_server_is_left_to_the_app_path(monkeypatch: pytest.MonkeyPatch) -> None:
    config, _ = _project_one(
        monkeypatch, "myapp:srv", in_scope=False, app_owned={"myapp:srv": True}
    )
    assert config["mcpServers"]["myapp:srv"]["env"]["API_TOKEN"] == REF


def test_a_server_without_a_reference_does_not_read_the_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> frozenset[str]:
        raise AssertionError("routing read for a server with no secret reference")

    spec = {"command": "plain-mcp", "env": {"MODE": "fast"}}
    config = {"mcpServers": {"plain": dict(spec)}}
    sources = mcp_sources.McpSources(
        kirocrew={"plain": spec}, kiro_global={}, provider_global={}, managed_names=set()
    )
    monkeypatch.setattr(mcp_sources, "_gateway_routed_names", _boom)
    mcp_sources.resolve_mcp_servers(config, sources, lambda cmd, _env: (f"/opt/bin/{cmd}", ""))
    assert config["mcpServers"]["plain"]["env"] == {"MODE": "fast"}


def test_an_unreadable_routing_withholds_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: a config that cannot be read routes nothing."""

    def _broken_load() -> None:
        raise OSError("config unreadable")

    import kiro_crew.config.loader as loader

    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(_broken_load))
    assert mcp_sources._gateway_routed_names() == frozenset()


# --- rewriter: a rostered server is never handed out raw ---------------------


def _rewrite(spec: dict, tmp_path: Path, **kw: object) -> tuple[dict, int]:
    return _rewrite_single_spec(
        spec,
        stubs_dir=tmp_path / "stubs",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        **kw,  # type: ignore[arg-type]
    )


def test_a_credential_reference_gets_a_private_backend_instead_of_none(tmp_path: Path) -> None:
    """A credential-named key is withheld from a SHARED backend, and an unwrapped
    server would be launched by the session with the literal reference. A private
    backend receives the whole declared env, which is where the gateway resolves it."""
    spec = {
        "name": "agent-a",
        "mcpServers": {"atl": {"command": sys.executable, "env": {"OAUTH_TOKEN": REF}}},
    }
    out, wrapped = _rewrite(
        spec, tmp_path, stub_servers=frozenset({"atl"}), pooling_enabled=True, forward_env=True
    )
    entry = out["mcpServers"]["atl"]
    assert wrapped == 1
    assert entry.get(_WRAPPER_MARKER) is True
    assert "--poolable" not in expand_stub_flags(entry["args"])


def test_a_withheld_plain_credential_still_runs_unwrapped(tmp_path: Path) -> None:
    """Unchanged for a literal credential: the session launches it with its env."""
    spec = {
        "name": "agent-a",
        "mcpServers": {"atl": {"command": sys.executable, "env": {"OAUTH_TOKEN": "x"}}},
    }
    out, wrapped = _rewrite(
        spec, tmp_path, stub_servers=frozenset({"atl"}), pooling_enabled=True, forward_env=True
    )
    assert wrapped == 0
    assert out["mcpServers"]["atl"].get("env") == {"OAUTH_TOKEN": "x"}
    assert "disabled" not in out["mcpServers"]["atl"]


def test_a_settings_server_with_a_credential_reference_gets_a_private_backend(
    tmp_path: Path,
) -> None:
    """The settings-file edition of the same rule: the server is injected as a
    private stub rather than left to kiro-cli's own merge, which would launch it
    from the session with the literal reference."""
    entry = {"command": sys.executable, "env": {"OAUTH_TOKEN": REF}}
    inject = _injectable_settings_servers(
        {"mcpServers": {"atl": entry}},
        frozenset({"atl"}),
        pooling_enabled=True,
        forward_env=True,
    )
    assert set(inject) == {"atl"}
    out, wrapped = _rewrite(
        {"name": "agent-a", "mcpServers": {}},
        tmp_path,
        stub_servers=frozenset({"atl"}),
        pooling_enabled=True,
        forward_env=True,
        inject_servers=inject,
    )
    assert wrapped == 1
    assert out["mcpServers"]["atl"].get(_WRAPPER_MARKER) is True
    assert "--poolable" not in expand_stub_flags(out["mcpServers"]["atl"]["args"])


def test_a_settings_server_with_a_plain_credential_is_still_left_to_kiro_cli() -> None:
    entry = {"command": sys.executable, "env": {"OAUTH_TOKEN": "x"}}
    inject = _injectable_settings_servers(
        {"mcpServers": {"atl": entry}},
        frozenset({"atl"}),
        pooling_enabled=True,
        forward_env=True,
    )
    assert inject == {}


class _RefuseAll:
    """A launch-approval store that approves nothing."""

    def admit(self, *_a: object, **_kw: object) -> bool:
        return False


def _ref_spec(name: str = "atl", command: str = sys.executable, env: dict | None = None) -> dict:
    return {
        "name": "agent-a",
        "mcpServers": {name: {"command": command, "env": env or {"API_KEY": REF}}},
    }


def test_an_unapproved_routed_server_with_a_reference_stays_wrapped(tmp_path: Path) -> None:
    """Left unwrapped, the session would launch the spec's entry with the literal
    reference. Wrapped, the gateway has no approved target for it and refuses."""
    out, wrapped = _rewrite(
        _ref_spec(), tmp_path, stub_servers=frozenset({"atl"}), approvals=_RefuseAll()
    )
    assert wrapped == 1
    assert out["mcpServers"]["atl"].get(_WRAPPER_MARKER) is True
    assert "--poolable" not in expand_stub_flags(out["mcpServers"]["atl"]["args"])


def test_the_routing_probe_lifts_the_rebuilds_withheld_mute(tmp_path: Path) -> None:
    """The launch probe routes a server before its routing is saved, so the
    agent spec still carries the rebuild's mute. It must be wrapped (and its
    launch captured for approval), not passed through as the user's mute."""
    spec = _ref_spec()
    spec["mcpServers"]["atl"].update({"disabled": True, mcp_sources.WITHHELD_KEY: True})
    out, wrapped = _rewrite(spec, tmp_path, stub_servers=frozenset({"atl"}))
    assert wrapped == 1
    assert out["mcpServers"]["atl"].get(_WRAPPER_MARKER) is True


def test_a_user_mute_is_still_honoured_by_the_rewriter(tmp_path: Path) -> None:
    spec = _ref_spec()
    spec["mcpServers"]["atl"]["disabled"] = True
    out, wrapped = _rewrite(spec, tmp_path, stub_servers=frozenset({"atl"}))
    assert wrapped == 0
    assert out["mcpServers"]["atl"]["disabled"] is True


def test_an_unrouted_withheld_mute_stays_muted_in_the_overlay(tmp_path: Path) -> None:
    spec = _ref_spec()
    spec["mcpServers"]["atl"].update({"disabled": True, mcp_sources.WITHHELD_KEY: True})
    out, wrapped = _rewrite(spec, tmp_path, stub_servers=frozenset())
    assert wrapped == 0
    assert out["mcpServers"]["atl"]["disabled"] is True


def test_a_servable_routed_server_with_a_reference_stays_pooled(tmp_path: Path) -> None:
    """Nothing withheld, command resolves, launch approved: the pooled spawn
    resolves the reference itself, so the server keeps sharing one backend."""
    out, wrapped = _rewrite(
        _ref_spec(env={"API_URL": REF}),
        tmp_path,
        stub_servers=frozenset({"atl"}),
        pooling_enabled=True,
        forward_env=True,
    )
    assert wrapped == 1
    assert "--poolable" in expand_stub_flags(out["mcpServers"]["atl"]["args"])


def test_an_unapproved_routed_server_without_a_reference_is_still_left_unwrapped(
    tmp_path: Path,
) -> None:
    out, wrapped = _rewrite(
        _ref_spec(env={"MODE": "x"}),
        tmp_path,
        stub_servers=frozenset({"atl"}),
        approvals=_RefuseAll(),
    )
    assert wrapped == 0
    assert out["mcpServers"]["atl"].get(_WRAPPER_MARKER) is None


def test_an_unresolvable_routed_server_with_a_reference_stays_wrapped(tmp_path: Path) -> None:
    out, wrapped = _rewrite(
        _ref_spec(command="no-such-mcp-binary-16189"),
        tmp_path,
        stub_servers=frozenset({"atl"}),
    )
    assert wrapped == 1
    assert out["mcpServers"]["atl"].get(_WRAPPER_MARKER) is True


def test_a_server_routed_under_its_raw_name_is_wrapped_under_its_alias(tmp_path: Path) -> None:
    """The spec key is the alias; the routing list holds the raw slashed name."""
    out, wrapped = _rewrite(
        _ref_spec(name="acme-mcp"), tmp_path, stub_servers=frozenset({"npm:@acme/mcp"})
    )
    assert wrapped == 1
    assert out["mcpServers"]["acme-mcp"].get(_WRAPPER_MARKER) is True


def test_a_settings_server_with_a_reference_is_injected_when_unresolvable(
    tmp_path: Path,
) -> None:
    entry = {"command": "no-such-mcp-binary-16189", "env": {"API_KEY": REF}}
    inject = _injectable_settings_servers({"mcpServers": {"atl": entry}}, frozenset({"atl"}))
    assert set(inject) == {"atl"}
    out, wrapped = _rewrite(
        {"name": "agent-a", "mcpServers": {}},
        tmp_path,
        stub_servers=frozenset({"atl"}),
        inject_servers=inject,
        approvals=_RefuseAll(),
    )
    assert wrapped == 1
    assert out["mcpServers"]["atl"].get(_WRAPPER_MARKER) is True


# --- stub fallback: never exec with an unresolved reference -------------------


def _fallback_args(tmp_path: Path, declared: dict) -> object:
    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps(declared), encoding="utf-8")
    return stub_mod._parse_args(
        [
            "--server",
            "atl",
            "--agent",
            "agent-a",
            "--target-command",
            "true",
            "--work-dir",
            str(tmp_path),
            "--env-file",
            str(env_file),
        ]
    )


def test_the_fallback_refuses_to_exec_with_a_secret_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _exec(*_a: object) -> None:
        raise AssertionError("exec'd a backend with an unresolved secret reference")

    monkeypatch.setattr(stub_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(stub_mod.os, "execvpe", _exec)
    with pytest.raises(SystemExit) as exc:
        stub_mod.fallback_exec(_fallback_args(tmp_path, {"API_TOKEN": REF}))
    assert exc.value.code == stub_mod._EXIT_UNRESOLVED_SECRET
    err = capsys.readouterr().err
    assert "'API_TOKEN'" in err and "ATLASSIAN_TOKEN" not in err


def test_the_fallback_still_execs_a_plain_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, dict[str, str]] = {}

    def _capture(_argv: str, _args: list[str], env: dict[str, str]) -> None:
        seen["env"] = dict(env)
        raise SystemExit(0)

    monkeypatch.setattr(stub_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(stub_mod.os, "execvpe", _capture)
    with pytest.raises(SystemExit):
        stub_mod.fallback_exec(_fallback_args(tmp_path, {"API_TOKEN": "literal"}))
    assert seen["env"]["API_TOKEN"] == "literal"


def test_the_fallback_ignores_an_inherited_value_it_did_not_declare(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, dict[str, str]] = {}

    def _capture(_argv: str, _args: list[str], env: dict[str, str]) -> None:
        seen["env"] = dict(env)
        raise SystemExit(0)

    monkeypatch.setenv("UNRELATED_SETTING", "secret://not-ours")
    monkeypatch.setattr(stub_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(stub_mod.os, "execvpe", _capture)
    with pytest.raises(SystemExit) as exc:
        stub_mod.fallback_exec(_fallback_args(tmp_path, {"API_TOKEN": "literal"}))
    assert exc.value.code == 0
    assert seen["env"]["API_TOKEN"] == "literal"
