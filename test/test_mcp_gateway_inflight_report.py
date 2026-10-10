"""The gateway half of the in-flight report (``mcp_caller.INFLIGHT_NOTIFICATION``).

A pooled backend parks its long calls as deferred tools and reports the ids it
is still making progress on. The gateway asks for that report in ``initialize``,
consumes it off the backend's stdout (never routing it to a stub), ages a
reported request from its last report, and holds a reporting backend to the
short ``PROGRESS_WEDGE_CEILING_SECS`` instead of the 3-hour ceiling -- which is
what lets a genuinely hung call recycle in minutes (#17031).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest
from test_mcp_gateway_wedge_ping_gate import _make_backend

from kiro_crew.mcp_caller import INFLIGHT_CAPABILITY_KEY, INFLIGHT_NOTIFICATION
from kiro_crew.mcp_gateway import hazards
from kiro_crew.mcp_gateway.backend import (
    HARD_WEDGE_CEILING_SECS,
    HEARTBEAT_TIMEOUT_SECS,
    PROGRESS_WEDGE_CEILING_SECS,
    _inject_inflight_capability,
    _PendingRequest,
    _reports_inflight,
)


def _pending(age_secs: float, now: float) -> _PendingRequest:
    return _PendingRequest(
        stub_uuid="stub-A",
        original_id=1,
        method="tools/call",
        t_start_ms=(now - age_secs) * 1000.0,
    )


class TestCapabilityHandshake:
    def test_forwarded_initialize_asks_for_the_report(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"roots": {}}, "clientInfo": {"name": "kiro-cli"}},
        }
        out = _inject_inflight_capability(msg)
        assert out["params"]["capabilities"]["experimental"] == {INFLIGHT_CAPABILITY_KEY: {}}
        assert out["params"]["capabilities"]["roots"] == {}
        # Copy discipline: the stub's frame is never aliased.
        assert "experimental" not in msg["params"]["capabilities"]

    def test_existing_experimental_entries_are_kept(self):
        msg = {"params": {"capabilities": {"experimental": {"other": {"x": 1}}}}}
        out = _inject_inflight_capability(msg)
        assert out["params"]["capabilities"]["experimental"] == {
            "other": {"x": 1},
            INFLIGHT_CAPABILITY_KEY: {},
        }

    def test_non_initialize_shapes_pass_through(self):
        assert _inject_inflight_capability({"params": "nope"}) == {"params": "nope"}
        assert _inject_inflight_capability({}) == {}

    def test_reports_inflight_reads_the_advertisement(self):
        assert _reports_inflight({"capabilities": {"experimental": {INFLIGHT_CAPABILITY_KEY: {}}}})
        assert not _reports_inflight({"capabilities": {"experimental": {}}})
        assert not _reports_inflight({"capabilities": {}})
        assert not _reports_inflight({})


class TestReportConsumption:
    @pytest.mark.asyncio
    async def test_report_refreshes_progress_and_is_not_routed_or_recorded(self):
        now = time.monotonic()
        backend = _make_backend(
            pending={"gw-9999-1": _pending(600, now), "gw-9999-2": _pending(600, now)}
        )
        backend._record_hazard = MagicMock()  # type: ignore[method-assign]
        line = (
            '{"jsonrpc": "2.0", "method": "%s", "params": {"requestIds": ["gw-9999-1", "gw-unknown"]}}\n'
            % INFLIGHT_NOTIFICATION
        ).encode()
        await backend._route_backend_line(line)
        refreshed = backend._pending_requests["gw-9999-1"].t_progress_ms
        assert refreshed >= now * 1000.0
        assert backend._pending_requests["gw-9999-2"].t_progress_ms == 0.0
        backend._record_hazard.assert_not_called()
        assert backend._test_inbox.empty(), "a gateway-internal frame reached a stub"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_malformed_report_refreshes_nothing(self):
        now = time.monotonic()
        backend = _make_backend(pending={"gw-9999-1": _pending(600, now)})
        for params in ('{"requestIds": "gw-9999-1"}', "{}", "[]", "null"):
            line = (
                '{"jsonrpc": "2.0", "method": "%s", "params": %s}\n'
                % (INFLIGHT_NOTIFICATION, params)
            ).encode()
            await backend._route_backend_line(line)
        assert backend._pending_requests["gw-9999-1"].t_progress_ms == 0.0


class TestProgressAwareWedgeDetection:
    @pytest.mark.asyncio
    async def test_a_reported_request_is_aged_from_its_report(self):
        """Started an hour ago, reported 10s ago: not slow, not wedged."""
        now = time.monotonic()
        pending = _pending(3600, now)
        pending.t_progress_ms = (now - 10) * 1000.0
        backend = _make_backend(pending={"gw-9999-1": pending}, last_ping_response_mono=now - 30)
        backend.reports_inflight = True
        assert await backend._heartbeat_once(now=now) == "alive"
        assert "gw-9999-1" not in backend._warned_slow_ids

    @pytest.mark.asyncio
    async def test_reporting_backend_recycles_at_the_short_ceiling(self):
        """On a reporting backend an unrefreshed age past PROGRESS_WEDGE_CEILING_SECS
        is a call that stopped progressing: recycled even with fresh pings."""
        now = time.monotonic()
        backend = _make_backend(
            pending={"gw-9999-1": _pending(PROGRESS_WEDGE_CEILING_SECS + 1, now)},
            last_ping_response_mono=now - 5,
        )
        backend.reports_inflight = True
        assert await backend._heartbeat_once(now=now) == "wedged"
        assert backend._dead_reason is not None
        assert f"{PROGRESS_WEDGE_CEILING_SECS:.0f}s hard ceiling" in backend._dead_reason

    @pytest.mark.asyncio
    async def test_legacy_backend_keeps_the_long_ceiling(self):
        """A backend that never advertised the report (an older install, the
        Windows loop) cannot vouch for its calls, so the 3h ceiling stays."""
        now = time.monotonic()
        backend = _make_backend(
            pending={"gw-9999-1": _pending(PROGRESS_WEDGE_CEILING_SECS + 1, now)},
            last_ping_response_mono=now - 5,
        )
        assert backend.reports_inflight is False
        assert await backend._heartbeat_once(now=now) == "alive"
        assert "gw-9999-1" in backend._warned_slow_ids

    @pytest.mark.asyncio
    async def test_a_stale_report_does_not_outlive_the_short_ceiling(self):
        """Reported once, long ago: the refreshed age still crosses the ceiling."""
        now = time.monotonic()
        pending = _pending(7200, now)
        pending.t_progress_ms = (now - PROGRESS_WEDGE_CEILING_SECS - 1) * 1000.0
        backend = _make_backend(pending={"gw-9999-1": pending}, last_ping_response_mono=now - 5)
        backend.reports_inflight = True
        assert await backend._heartbeat_once(now=now) == "wedged"

    def test_ceiling_relationships(self):
        """Above the slow-request threshold (it is a backstop, not the detector)
        and well under the legacy ceiling."""
        assert HEARTBEAT_TIMEOUT_SECS < PROGRESS_WEDGE_CEILING_SECS < HARD_WEDGE_CEILING_SECS

    def test_ceiling_outlives_every_synchronous_tool_bound(self):
        """A kirocrew-core tool that still runs on the worker bounds every call
        it makes; the short ceiling must sit above the LONGEST of those with the
        slow-request margin, or a reporting backend is recycled under a healthy
        call. Read from the owning constants, not restated: raising one of them
        past the ceiling is exactly what should fail here."""
        from kiro_crew.mcp_tools import apps, browser, knowledge
        from kiro_crew.validation import ASK_WAIT_SLICE_SECS

        synchronous_bounds = {
            "pod_up": apps._POD_UP_TIMEOUT_S,
            "pod_down": apps._POD_DOWN_TIMEOUT_S,
            "pod read": apps._POD_READ_TIMEOUT_S,
            "browser command": browser.COMMAND_TIMEOUT_SECS,
            "knowledge_add_document": knowledge._ADD_DOCUMENT_TIMEOUT_SECS,
            "ask slice (one deferred step)": ASK_WAIT_SLICE_SECS + 15,
        }
        longest = max(synchronous_bounds, key=synchronous_bounds.get)  # type: ignore[arg-type]
        assert (
            PROGRESS_WEDGE_CEILING_SECS >= synchronous_bounds[longest] + HEARTBEAT_TIMEOUT_SECS
        ), (
            f"{longest} runs for {synchronous_bounds[longest]}s synchronously; the ceiling "
            f"must leave it the slow-request margin"
        )


@pytest.mark.asyncio
async def test_unknown_notification_still_records_a_hazard():
    """The inflight arm is a narrow carve-out: any other unattributable
    request-scoped notification is still dropped and recorded."""
    backend = _make_backend()
    backend._record_hazard = MagicMock()  # type: ignore[method-assign]
    await backend._route_backend_line(
        b'{"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": "zz"}}\n'
    )
    backend._record_hazard.assert_called_once_with(hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)
    await asyncio.sleep(0)
