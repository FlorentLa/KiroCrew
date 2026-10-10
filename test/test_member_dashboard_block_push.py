"""The v3 dashboard's push protocol, one test per sentence of it -- negatives included.

The five the brief fixes, and the two the conductor added:

1. First load renders the WHOLE page.
2. A fold pushes ONLY the blocks that subscribe to that fold.
3. It goes over the EXISTING WebSocket -- the owner channel -- carrying a version.
4. A version GAP, or a LAYOUT CHANGE, triggers a full refetch. Both, not just the gap.
5. The owner check and the redaction stay in the CONTROLLER.
6. A package-bound crewmate gets no builtin fallback; a registry-bound one is unchanged.
7. ``instance.RENDERABLE_SOURCES`` stays at one value -- the v3 page earns execution at
   its own gate, which is :func:`member_dashboard._minted_package_page`.

The redaction cases assert over the WHOLE serialized payload rather than the one field a
reader would think to look at, with a positive control proving the values that must
survive DID: a per-field assertion passes just as happily on an empty payload.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard.handlers import member_dashboard_push as push

pytestmark = pytest.mark.asyncio

MEMBER = "Fleet Conductor"
SLUG = "fleet-conductor"
BOUND = f"crewmate:{SLUG}"

#: A secret in the shape the egress redactor recognises, so the assertion is about the
#: controller's chokepoint rather than about a pattern invented here.
SECRET = "ghp_" + "A" * 36


def package(*, bound_to: str = BOUND, blocks: list[dict[str, Any]] | None = None) -> dict:
    """A package with TWO blocks reading TWO DIFFERENT folds.

    That split is the whole point of the fixture: a push for ``work`` must carry the
    ``prs`` block and must NOT carry ``runs``, and a fixture whose blocks share a fold
    could not tell a correct push from one that sends everything.
    """
    return {
        "kind": "dashboard",
        "bound_to": bound_to,
        "model": {
            "types": {
                "open_prs": {
                    "type": "number",
                    "label": "Open PRs",
                    "source": {"fold": "work", "path": "summary.open_prs"},
                },
                "note": {
                    "type": "text",
                    "label": "Note",
                    "source": {"fold": "work", "path": "summary.note"},
                },
                "last_run": {
                    "type": "timestamp",
                    "label": "Last run",
                    "source": {"fold": "panel", "path": "last_run"},
                },
            }
        },
        "view": {
            "blocks": (
                blocks
                if blocks is not None
                else [
                    {"id": "prs", "type": "table", "fields": ["open_prs", "note"]},
                    {"id": "runs", "type": "stat", "fields": ["last_run"]},
                ]
            )
        },
        "theme": {"tokens": {"--panel-bg": "oklch(21% 0 0)"}},
    }


class _Hub:
    """The hub's one method this push uses, recording what it was handed."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, dict[str, Any]]] = []

    def broadcast_ws_owners(self, msg_type: str, data: dict[str, Any]) -> None:
        # Serialized on the way in, exactly as the real hub does, so a payload that
        # cannot be JSON would fail here rather than in production.
        json.loads(json.dumps(data))
        self.frames.append((msg_type, data))


def _event(fold: str, value: dict[str, Any], *, revision: int = 1, seq: int = 1):
    from kiro_crew.crew_log import bus as crew_log_bus

    return crew_log_bus.FoldAdvanced(
        scope=crew_log_bus.SCOPE_SLOT, key=SLUG, fold=fold, revision=revision, value=value, seq=seq
    )


WORK = {"summary": {"open_prs": 4, "note": f"token {SECRET} pasted"}}
PANEL = {"last_run": "2026-10-09T23:00:00Z"}


@pytest.fixture(autouse=True)
def _env(tmp_path, _floor_monkeypatch):
    """An isolated home, a tmp artifact store, and the member checks stubbed.

    The member-identity and owner gates are the SIBLING routes' and are tested there;
    what is stubbed is config loading, not the checks. Patched through
    ``_floor_monkeypatch`` so a test body calling ``monkeypatch.undo()`` cannot hand the
    rest of that test the real config loader.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    cfg = SimpleNamespace(agents={MEMBER: SimpleNamespace(member_id="")})
    _floor_monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    _floor_monkeypatch.setattr(routes.members_mod, "member_slug", lambda name, config=None: SLUG)
    _floor_monkeypatch.setattr(routes.members_mod, "validate_slug", lambda slug: slug)
    _floor_monkeypatch.setattr(routes.members_mod, "is_dispatchable_member_name", bool)
    _floor_monkeypatch.setattr(routes, "_member_names_for_slug", lambda cfg, slug: [MEMBER])
    _floor_monkeypatch.setattr(routes, "_deny_app_caller", _none)
    _floor_monkeypatch.setattr(routes, "_owner_only", _none)
    _floor_monkeypatch.setattr(routes, "_write_session", lambda slug, member: "")
    yield
    push.close_all()


async def _none(*_a, **_k):
    return None


@pytest.fixture
def store(tmp_path, _floor_monkeypatch) -> ArtifactStore:
    """The process-wide default store, pointed at a tmp root for this test."""
    import kiro_crew.artifacts as artifacts_mod

    live = ArtifactStore(root=tmp_path / "artifacts")
    _floor_monkeypatch.setattr(artifacts_mod, "get_default_store", lambda: live)
    return live


@pytest.fixture
def saved(store: ArtifactStore):
    return store.create(name="Mate dashboard", kind="dashboard", content=json.dumps(package()))


def _model(store: ArtifactStore, slug: str):
    loaded = store.get(slug)
    return dp.model_of(dp.parse_package(loaded.content or ""), slug=slug, version=loaded.version)


def _page(model, hub: _Hub, *, reread=None) -> push.LivePage:
    """A live page with no loop: the send is awaited directly by the test."""
    return push.LivePage(
        SLUG,
        MEMBER,
        model,
        state=hub,
        loop=None,
        redact=routes._page_safe,
        reread=reread if reread is not None else (lambda _slug: model),
    )


#: A document declaring the policy the shipped renderer declares, so the gate is tested
#: against the real thing rather than against the one directive a substring check saw.
RENDER_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'"
)


def _document(policy: str = RENDER_CSP) -> str:
    return (
        "<!doctype html><html><head>"
        f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
        "</head><body><main>*{}</main></body></html>"
    )


@asynccontextmanager
async def _client(hub: _Hub | None = None):
    """A started client that always closes.

    An ``async with`` helper rather than a fixture, by this repo's convention: the
    pinned pytest-asyncio does not collect async-generator fixtures declared with a
    plain ``@pytest.fixture``. Closing is not tidiness -- a leaked TestClient costs two
    descriptors and fails UNRELATED tests with EMFILE on a loaded runner.
    """
    app = web.Application()
    if hub is not None:
        app["state"] = hub
    routes.register_member_dashboard_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


def _q() -> str:
    return f"/api/members/{SLUG}/dashboard?member={MEMBER.replace(' ', '+')}"


# --------------------------------------------------------------------------
# 1. first load renders the whole page
# --------------------------------------------------------------------------


async def test_the_first_load_carries_every_block(store, saved):
    """Every block, not the ones some fold happened to move."""
    hub = _Hub()
    async with _client(hub) as client:
        body = await (await client.get(_q())).json()
    assert body["state"] == "live"
    assert set(body["blocks"]) == {"prs", "runs"}
    assert body["package"]["slug"] == saved.slug
    assert body["package"]["version"] == saved.version
    # The counter the browser starts from. Without it the FIRST patch has nothing to be
    # compared against and a gap at version 1 would be undetectable.
    assert body["push_version"] == 0
    assert body["push_frame"] == push.BLOCK_FRAME


async def test_arming_a_page_does_not_push_the_values_its_own_body_carries(store, saved):
    """The subscribe-time baseline is cached, not pushed.

    The bus hands a baseline over synchronously inside ``subscribe``, and that value IS
    what the first load is about to put in its response. Pushing it sends the browser a
    patch carrying what it already has -- and makes the ``push_version`` in that
    response depend on whether the patch was broadcast before or after it was read.
    """
    hub = _Hub()
    async with _client(hub) as client:
        body = await (await client.get(_q())).json()
    assert hub.frames == [], "arming the page pushed a frame"
    assert body["push_version"] == 0


async def test_the_first_load_arms_the_push_before_it_reads(store, saved):
    """Armed by the read, so patches land on a page that got a full load to apply to."""
    hub = _Hub()
    async with _client(hub) as client:
        assert (await client.get(_q())).status == 200
    page = push.live_page(SLUG)
    assert page is not None
    assert page.member == MEMBER
    assert page.folds() == frozenset({"work", "panel"})


async def test_a_second_read_refreshes_one_page_rather_than_arming_a_second(store, saved):
    """Two sets of subscriptions would write two counters the browser cannot tell apart."""
    hub = _Hub()
    async with _client(hub) as client:
        await client.get(_q())
        first = push.live_page(SLUG)
        await client.get(_q())
        second = push.live_page(SLUG)
    assert first is second


# --------------------------------------------------------------------------
# 2. only the blocks that subscribe to the fold
# --------------------------------------------------------------------------


async def test_a_push_carries_only_the_blocks_that_subscribe_to_the_fold(store, saved):
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK))
    assert await page.send("work") is True
    _type, data = hub.frames[-1]
    assert set(data["blocks"]) == {"prs"}, "a block reading another fold was pushed"
    assert "runs" not in data["blocks"]
    assert data["fold"] == "work"


async def test_the_other_fold_pushes_the_other_block(store, saved):
    """The positive control for the case above: the split is real, not an empty result."""
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("panel", _event("panel", PANEL))
    assert await page.send("panel") is True
    _type, data = hub.frames[-1]
    assert set(data["blocks"]) == {"runs"}
    assert data["blocks"]["runs"] == ["last_run"]
    assert data["read"]["fields"]["last_run"] == PANEL["last_run"]


async def test_a_fold_under_a_key_kind_this_page_cannot_reach_is_reported_not_dropped(
    store, saved, monkeypatch
):
    """The seam with D4's third key kind.

    ``scope_for`` says which scope a fold is keyed by; ``bus_key`` says what the key IS,
    and only a SLOT key exists for a package today. A scope with no row there must be
    REPORTED unavailable rather than silently never delivered -- which is the whole
    difference between a merge that adds one row and a push that quietly loses a fold.
    """
    model = _model(store, saved.slug)
    page = _page(model, _Hub())
    monkeypatch.setattr(push, "scope_for", lambda fold: "tree")
    unavailable = page.subscribe()
    assert sorted(unavailable) == ["panel", "work"]
    assert page.bus_key("tree") == ""
    # NAMED, and nothing was subscribed under a scope with no key: the alternative is a
    # subscription that is registered and never delivers, which reads as a quiet page.
    assert page._disposers == []


async def test_a_slot_fold_is_keyed_by_the_crewmates_own_slug(store, saved):
    """The positive control for the case above: one key kind IS reachable."""
    from kiro_crew.crew_log import bus as crew_log_bus

    page = _page(_model(store, saved.slug), _Hub())
    assert page.bus_key(crew_log_bus.SCOPE_SLOT) == SLUG
    assert page.bus_key(crew_log_bus.SCOPE_SESSION) == ""


async def test_a_second_read_keeps_the_live_subscriptions_and_their_values(store, saved):
    """A poll must not empty the cache it is about to serve the full load from."""
    hub = _Hub()
    async with _client(hub) as client:
        await client.get(_q())
        page = push.live_page(SLUG)
        assert page is not None
        # Above whatever revision the subscribe-time baseline recorded, so this value is
        # the newer one rather than being dropped as an older arrival.
        page.on_fold("work", _event("work", WORK, revision=10_000))
        assert page.read()["fields"]["open_prs"] == 4, "the fixture never cached a value"
        body = await (await client.get(_q())).json()
    assert body["read"]["fields"]["open_prs"] == 4, "the second read resubscribed and lost it"


async def test_a_fold_no_block_subscribes_to_sends_nothing_and_spends_no_version(store, saved):
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    assert await page.send("ledger") is False
    assert hub.frames == []
    assert page.version == 0


async def test_a_field_whose_path_does_not_resolve_is_named_not_valued(store, saved):
    """A missing value is NAMED so the page dims that cell; a None would read as zero."""
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", {"summary": {"open_prs": 4}}))
    await page.send("work")
    _type, data = hub.frames[-1]
    assert data["blocks"]["prs"] == ["open_prs"]
    assert data["read"]["fields"] == {"open_prs": 4}
    assert data["missing"] == ["note"]


# --------------------------------------------------------------------------
# 3. the existing WebSocket, with a version
# --------------------------------------------------------------------------


async def test_the_push_uses_the_owner_channel_and_adds_no_second_one(store, saved):
    """One message TYPE on the hub's owner broadcast -- the one ``slot_projection`` uses."""
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK))
    await page.send("work")
    msg_type, _data = hub.frames[-1]
    assert msg_type == push.BLOCK_FRAME == "dashboard_block_patch"
    # An app token must never be handed this frame even if some later caller broadcasts
    # the name literally rather than through the owner-only method.
    from kiro_crew.dashboard import ws_event_scope

    assert push.BLOCK_FRAME in ws_event_scope._OWNER_ONLY_EVENTS


async def test_the_frame_carries_the_read_the_document_will_be_given(store, saved):
    """The page's own listener takes ``read`` and nothing else, so the frame carries it.

    ``dashboard_frame.read_payload`` builds it -- the same builder the v2 page and the
    renderer's data island use, so a first paint and a refill cannot describe one read
    differently.
    """
    from kiro_crew import dashboard_frame

    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK))
    await page.send("work")
    _type, data = hub.frames[-1]
    assert set(data["read"]) >= set(dashboard_frame.read_payload({}))
    assert data["read"]["fields"]["open_prs"] == 4
    assert data["read"]["agentic"] == []
    assert data["read"]["stale"] is True, "panel never moved, so its field is unresolved"
    assert "last_run" in data["read"]["missing"]


async def test_the_read_is_complete_even_though_the_push_is_partial(store, saved):
    """Forced by the document: its listener REPLACES what it holds.

    ``read = data.read`` in the page's message handler, so a read restricted to the
    pushed blocks would blank every cell outside them. Which blocks moved is carried
    separately in ``blocks``; narrowing the read needs the page to merge first.
    """
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK))
    page.on_fold("panel", _event("panel", PANEL))
    await page.send("work")
    _type, data = hub.frames[-1]
    assert set(data["blocks"]) == {"prs"}, "the push stopped being partial"
    # The field only the OTHER block reads is still in the read, with its value.
    assert data["read"]["fields"]["last_run"] == PANEL["last_run"]
    assert data["read"]["stale"] is False


async def test_the_display_strings_are_formatted_from_the_REDACTED_values(store, saved):
    """The order constraint, pinned on what the formatter was HANDED.

    A string formatted before the redactor ran would carry exactly what the redactor
    exists to mask, and it would sit beside the masked raw value where a reader is least
    likely to question it. So this asserts the seam SAW redacted input, not merely that
    the output happens to look clean -- the output could be clean by luck for one
    formatter and not for the next one.
    """
    hub = _Hub()
    model = _model(store, saved.slug)
    seen: list[Mapping[str, Any]] = []

    def _formatter(package, fields):
        seen.append(dict(fields))
        return {name: f"[{value}]" for name, value in fields.items()}

    page = push.LivePage(
        SLUG,
        MEMBER,
        model,
        state=hub,
        loop=None,
        redact=routes._page_safe,
        reread=lambda _slug: model,
        display_seam=_formatter,
    )
    page.on_fold("work", _event("work", WORK))
    await page.send("work")
    _type, data = hub.frames[-1]
    # WHAT THE FORMATTER WAS GIVEN.
    assert seen, "the formatter was never called"
    assert SECRET not in json.dumps(seen), "the formatter was handed the raw value"
    # AND THE WHOLE FRAME, which is where a display string formatted too early lands.
    assert SECRET not in json.dumps(data)
    assert SECRET not in json.dumps(data["read"]["display"])
    # POSITIVE CONTROL: the values that must survive DID, raw and formatted, so none of
    # the above is passing on an empty payload or a frame that never went out.
    assert data["read"]["fields"]["open_prs"] == 4
    assert data["read"]["display"]["open_prs"] == "[4]"
    assert "token" in data["read"]["display"]["note"]
    assert "REDACTED" in data["read"]["display"]["note"]


async def test_a_missing_field_gets_no_display_string_either(store, saved):
    """The page dims that cell. An empty string would render as a filled cell."""
    hub = _Hub()
    model = _model(store, saved.slug)
    page = push.LivePage(
        SLUG,
        MEMBER,
        model,
        state=hub,
        loop=None,
        redact=routes._page_safe,
        reread=lambda _slug: model,
        display_seam=lambda package, fields: {n: str(v) for n, v in fields.items()},
    )
    # `panel` never moved, so `last_run` does not resolve.
    page.on_fold("work", _event("work", WORK))
    read = page.read()
    assert "last_run" in read["missing"]
    assert "last_run" not in read["fields"]
    assert "last_run" not in read["display"], "a dimmed cell was given a string to show"
    # Positive control: the resolved field DID get one.
    assert read["display"]["open_prs"] == "4"


async def test_the_read_carries_the_renderers_own_formatting_when_it_is_there(store, saved):
    """Through the controller's seam, so a build without the renderer sends no display."""
    hub = _Hub()
    model = _model(store, saved.slug)
    bare = _page(model, hub)
    bare.on_fold("work", _event("work", WORK))
    assert "display" not in bare.read()

    page = push.LivePage(
        SLUG,
        MEMBER,
        model,
        state=hub,
        loop=None,
        redact=routes._page_safe,
        reread=lambda _slug: model,
        display_seam=lambda package, fields: {k: f"<{v}>" for k, v in fields.items()},
    )
    page.on_fold("work", _event("work", WORK))
    assert page.read()["display"]["open_prs"] == "<4>"


async def test_a_refetch_frame_carries_no_read_either(store, saved):
    """No values means none of them -- not "none in blocks and all of them in read"."""
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub, reread=lambda _slug: None)
    page.on_fold("work", _event("work", WORK))
    await page.send("work")
    _type, data = hub.frames[-1]
    assert data["refetch"] is True
    assert data["blocks"] == {}
    assert data["read"] == {}


async def test_the_body_names_the_message_type_the_document_accepts(store, saved):
    """Fixed by the repository, not invented here: the renderer imports this constant."""
    from kiro_crew import dashboard_frame

    hub = _Hub()
    async with _client(hub) as client:
        body = await (await client.get(_q())).json()
    assert body["page_message"] == dashboard_frame.DATA_MESSAGE_TYPE
    assert body["read"]["fields"] == body["read"]["fields"]  # present and serializable
    assert set(body["read"]) >= {"fields", "missing", "stale", "agentic"}


async def test_the_version_steps_by_exactly_one_per_frame(store, saved):
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    for revision in (1, 2, 3):
        page.on_fold("work", _event("work", WORK, revision=revision))
        await page.send("work")
    assert [data["version"] for _t, data in hub.frames] == [1, 2, 3]


async def test_a_frame_names_the_layout_it_was_computed_under(store, saved):
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK))
    await page.send("work")
    _type, data = hub.frames[-1]
    assert data["layout"] == saved.version


async def test_an_older_revision_is_dropped_rather_than_pushed(store, saved):
    """Ordered by REVISION, never by arrival: a seq can move down while a value moves on."""
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK, revision=5))
    await page.send("work")
    page.on_fold("work", _event("work", {"summary": {"open_prs": 999}}, revision=2))
    await page.send("work")
    assert hub.frames[-1][1]["read"]["fields"]["open_prs"] == 4


# --------------------------------------------------------------------------
# 4. a gap OR a layout change -> refetch. Both.
# --------------------------------------------------------------------------


async def test_a_layout_change_sends_a_refetch_carrying_no_values(store, saved):
    """The block set may have moved, so values under the new layout must not be applied."""
    hub = _Hub()
    model = _model(store, saved.slug)
    recomposed = store.update(
        saved.slug,
        content=json.dumps(package(blocks=[{"id": "prs", "type": "stat", "fields": ["open_prs"]}])),
    )
    assert recomposed.version != saved.version, "the fixture did not change the layout"
    page = _page(model, hub, reread=lambda _slug: _model(store, saved.slug))
    page.on_fold("work", _event("work", WORK))
    assert await page.send("work") is True
    _type, data = hub.frames[-1]
    assert data["refetch"] is True
    assert data["reason"] == push.REASON_LAYOUT
    assert data["blocks"] == {}
    assert data["layout"] == recomposed.version


async def test_the_gap_a_restart_makes_is_a_gap_the_page_can_see(store, saved):
    """The version the page holds and the one a fresh page sends cannot be confused.

    A gateway restarted inside one interpreter arms a NEW page whose counter starts at
    zero, so its first frame carries version 1 while the browser holds 7. The rule the
    page applies is ``held + 1``, not ``> held``, which is why that reads as a gap and
    refetches rather than being applied over newer values.
    """
    hub = _Hub()
    model = _model(store, saved.slug)
    old = _page(model, hub)
    for revision in range(1, 8):
        old.on_fold("work", _event("work", WORK, revision=revision))
        await old.send("work")
    assert hub.frames[-1][1]["version"] == 7
    fresh = _page(model, hub)
    fresh.on_fold("work", _event("work", WORK))
    await fresh.send("work")
    assert hub.frames[-1][1]["version"] == 1


# --------------------------------------------------------------------------
# 5. the owner check and the redaction stay in the controller
# --------------------------------------------------------------------------


async def test_a_package_this_member_does_not_own_is_not_pushed(store, tmp_path):
    """The negative one. A package bound elsewhere arms nothing and sends nothing."""
    other = store.create(
        name="Someone else",
        kind="dashboard",
        content=json.dumps(package(bound_to="crewmate:other")),
    )
    hub = _Hub()
    model = _model(store, other.slug)
    request = SimpleNamespace(app={"state": hub})
    assert routes.arm_block_push(request, SLUG, MEMBER, model) is None
    assert push.live_page(SLUG) is None
    assert hub.frames == []


async def test_a_session_bound_package_is_not_a_members(store):
    """A slot key and a member slug look alike, which is the case a loose check misses."""
    model = _model(
        store,
        store.create(
            name="Slot page",
            kind="dashboard",
            content=json.dumps(package(bound_to=f"session:{SLUG}")),
        ).slug,
    )
    assert routes._owns_package(SLUG, model) is False


async def test_a_rebind_stops_the_push_and_asks_for_a_refetch(store, saved):
    """A rebind writes no version, so the layout check cannot see it: the re-read does."""
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub, reread=lambda _slug: None)
    page.on_fold("work", _event("work", WORK))
    assert await page.send("work") is True
    _type, data = hub.frames[-1]
    assert data["refetch"] is True
    assert data["reason"] == push.REASON_UNBOUND
    assert data["blocks"] == {}
    # And it is over for good: a closed page never sends another frame.
    page.on_fold("work", _event("work", WORK, revision=9))
    assert await page.send("work") is False
    assert len(hub.frames) == 1


async def test_a_redacted_value_is_redacted_in_the_whole_pushed_payload(store, saved):
    """Asserted over the WHOLE serialized frame, not the one field a reader would check.

    The same value can reappear on a block id, a key, or a sibling field, and a
    per-field assertion would not see it.
    """
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK))
    await page.send("work")
    _type, data = hub.frames[-1]
    serialized = json.dumps(data)
    assert SECRET not in serialized
    # Both halves of the frame carry values, so BOTH have to be covered -- `blocks` and
    # the `read` the document is actually given. The whole-payload assertion above is
    # what catches the second one; these name them so a reader can see it did.
    assert SECRET not in json.dumps(data["blocks"])
    assert SECRET not in json.dumps(data["read"])
    # POSITIVE CONTROL: the values that must survive DID, so this is not passing on an
    # empty payload or a frame that never went out.
    assert data["blocks"]["prs"] == ["open_prs", "note"]
    assert data["read"]["fields"]["open_prs"] == 4


async def test_the_push_cannot_be_armed_without_the_controllers_redactor():
    """Refused rather than defaulted: a default here would be a SECOND redactor."""
    with pytest.raises(TypeError):
        push.LivePage(
            SLUG,
            MEMBER,
            SimpleNamespace(),
            state=None,
            loop=None,
            redact=None,
            reread=lambda s: None,
        )


async def test_the_controller_is_what_hands_the_push_its_redactor(store, saved):
    hub = _Hub()
    request = SimpleNamespace(app={"state": hub})
    page = routes.arm_block_push(request, SLUG, MEMBER, _model(store, saved.slug))
    assert page is not None
    assert page._redact is routes._page_safe


# --------------------------------------------------------------------------
# 6. the builtin fallback is scoped by SOURCE (conductor's cycle-2 ruling)
# --------------------------------------------------------------------------


async def test_a_package_bound_member_gets_no_builtin_fallback(store, saved, monkeypatch):
    """v3 has no default page, so a package-bound crewmate is never handed a shipped one.

    The bound-but-unreadable case, which is the only one that can reach the fallback at
    all: a servable package returns above it.
    """
    called: list[str] = []

    def _default(slug: str):
        called.append(slug)
        raise AssertionError("a package-bound member must not be handed a builtin page")

    monkeypatch.setattr(routes.instance, "default_instance", _default)
    monkeypatch.setattr(routes, "_read_package", lambda slug: routes._Packaged(bound=True))
    hub = _Hub()
    async with _client(hub) as client:
        body = await (await client.get(_q())).json()
    assert called == []
    assert body["state"] == "empty"
    assert "rendered_html" not in body


async def test_a_registry_bound_member_still_gets_the_default(store, monkeypatch):
    """The other direction. The shipped templates are a live feature and stay one."""
    fallback = SimpleNamespace(
        state=routes.instance.STATE_EMPTY,
        wire=lambda: {"instance_version": 0, "state": "empty", "html": "<p>x</p>", "manifest": {}},
    )
    seen: list[str] = []

    def _default(slug: str):
        seen.append(slug)
        return fallback

    monkeypatch.setattr(routes.instance, "default_instance", _default)
    monkeypatch.setattr(routes, "_render", lambda *a, **k: "<html>composed</html>")
    async with _client() as client:
        body = await (await client.get(_q())).json()
    assert seen == [SLUG], "the registry fallback stopped being offered"
    assert body["rendered_html"] == "<html>composed</html>"


async def test_the_read_tells_bound_and_unreadable_from_not_bound(store, saved, monkeypatch):
    """THREE states, and the third is the one the ruling turns on.

    The third is reached the way it is reached in production: the binding scan parses
    the record and matches it, and the read AFTER that one fails -- the content changed
    under the read, or the store faulted between the two.
    """
    assert routes._read_package("nobody") == routes._Packaged(bound=False)
    answered = routes._read_package(SLUG)
    assert answered.bound is True and answered.model is not None

    real = dp.parse_package
    calls: list[int] = []

    def _fails_after_the_scan(content: str):
        calls.append(1)
        if len(calls) == 1:
            return real(content)
        raise ValueError("the content moved under the read")

    monkeypatch.setattr(dp, "parse_package", _fails_after_the_scan)
    broken = routes._read_package(SLUG)
    assert len(calls) == 2, "the fixture did not reach the read after the scan"
    assert broken.bound is True, "a package bound here read as absent"
    assert broken.model is None


async def test_a_failed_package_scan_suppresses_the_fallback_rather_than_guessing(
    store, saved, monkeypatch
):
    """An unknown answer must not hand a crewmate a page they did not compose."""
    monkeypatch.setattr(dp, "resolve_bound_slug", _raise)
    answered = routes._read_package(SLUG)
    assert answered.bound is True
    assert answered.model is None


def _raise(*_a, **_k):
    raise ValueError("not a package")


# --------------------------------------------------------------------------
# 7. the v3 page earns execution at its OWN gate
# --------------------------------------------------------------------------


async def test_the_shipped_policy_passes_the_gate(monkeypatch):
    """The positive control. The real `RENDER_CSP` is what the gate must admit."""
    monkeypatch.setattr(routes, "_package_render", lambda name: lambda *a, **k: _document())
    assert routes._minted_package_page(SLUG, package(), {}) is not None


@pytest.mark.parametrize(
    "policy",
    [
        pytest.param("", id="no-policy-at-all"),
        pytest.param(
            RENDER_CSP.replace("connect-src 'none'", "connect-src https://example.test"),
            id="can-reach-the-network",
        ),
        pytest.param(RENDER_CSP + "; script-src 'unsafe-eval'", id="can-compile-what-it-is-given"),
        pytest.param(
            RENDER_CSP.replace("form-action 'none'", "form-action *"), id="can-post-somewhere"
        ),
        pytest.param(
            RENDER_CSP.replace("img-src data:", "img-src http:"), id="can-exfiltrate-by-image"
        ),
        pytest.param(
            RENDER_CSP.replace("base-uri 'none'", "base-uri https://example.test"),
            id="can-move-its-own-base",
        ),
    ],
)
async def test_a_document_that_could_carry_the_numbers_somewhere_is_refused(
    monkeypatch, policy: str
):
    """The gate is the list of properties the safety argument rests on, not one string.

    Every case here CONTAINS ``default-src 'none'`` -- the shipped policy starts with
    exactly those bytes -- so a substring check for it would pass all of them while the
    justification beside it read as false.
    """
    body = "<html><head></head><body></body></html>" if not policy else _document(policy)
    monkeypatch.setattr(routes, "_package_render", lambda name: lambda *a, **k: body)
    assert routes._minted_package_page(SLUG, package(), {}) is None


async def test_the_gate_names_what_is_wrong_with_the_policy():
    """A refusal a reviewer can act on: which directive, not that something failed."""
    assert routes._csp_problems(_document()) == []
    assert routes._csp_problems("<html></html>") == ["declares no Content-Security-Policy"]
    missing = routes._csp_problems(_document("default-src 'none'"))
    assert "is missing \"connect-src 'none'\"" in missing
    assert routes._csp_problems(_document(RENDER_CSP + "; script-src 'unsafe-eval'")) == [
        "grants \"'unsafe-eval'\""
    ]


async def test_a_block_type_outside_the_closed_catalogue_is_not_rendered(monkeypatch):
    """Re-checked at the RENDER site, like ``_trusted_page`` not trusting a stored label."""
    rendered: list[str] = []

    def _seam(_name):
        def _render(*_a, **_k):
            rendered.append("ran")
            return _document()

        return _render

    monkeypatch.setattr(routes, "_package_render", _seam)
    pkg = package()
    pkg["view"]["blocks"] = [{"id": "prs", "type": "iframe", "fields": ["open_prs"]}]
    assert routes._minted_package_page(SLUG, pkg, {}) is None
    assert rendered == [], "the renderer ran for a block type nothing in the repo defines"


async def test_the_renderer_seam_is_the_module_the_renderer_actually_lands_in(monkeypatch):
    """Named once, so the merge is one line -- and named CORRECTLY, so it is not zero.

    A seam pointing at a module that never arrives resolves to None forever and the
    first load silently serves no document, which is indistinguishable from a build
    that has not merged the renderer yet.
    """
    assert routes._PACKAGE_RENDER_MODULE == "kiro_crew.dashboard_package_render"
    # The two names this controller asks that module for.
    import types

    fake = types.ModuleType(routes._PACKAGE_RENDER_MODULE)
    fake.render_dashboard = lambda package, read, *, theme="light", title="": _document()
    fake.display_values = lambda package, fields: {k: str(v) for k, v in fields.items()}
    fake.THEMES = ("light", "dark")
    monkeypatch.setitem(__import__("sys").modules, routes._PACKAGE_RENDER_MODULE, fake)
    assert routes._package_render("render_dashboard") is fake.render_dashboard
    assert routes._package_render("display_values") is fake.display_values
    assert routes._package_render_value("THEMES") == ("light", "dark")
    assert routes._package_render("nothing_of_that_name") is None


async def test_a_package_page_never_reaches_the_template_execution_gate():
    """Which is WHY ``instance.RENDERABLE_SOURCES`` stays at one value.

    ``_trusted_page`` is the gate that set guards, and it answers by loading markup from
    the template CATALOG by the record's ``template_id``. A package has no catalog
    directory and carries no markup key at all, so widening the set would loosen the
    template adopt path (``instance._check``) and buy this page nothing.
    """
    from kiro_crew.dashboard_templates import catalog
    from kiro_crew.dashboard_templates import instance as instance_store

    assert instance_store.RENDERABLE_SOURCES == frozenset({catalog.BUILTIN_SOURCE})
    assert "package" not in instance_store.RENDERABLE_SOURCES
    # The structural half of the claim: a validated package has no key that can carry
    # markup or script, so there is no stored page for that gate to be asked about.
    assert set(dp.validate_package(package())) == {"kind", "bound_to", "model", "view", "theme"}
