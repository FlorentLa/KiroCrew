"""The v3 dashboard's push: one open page's fold subscriptions in, block patches out.

The protocol this module implements, in the five sentences the brief fixes:

1. **First load renders the whole page.** The controller's
   ``GET /api/members/{slug}/dashboard`` serves every block's values, and ARMS one
   :class:`LivePage` for the crewmate as it does so. The body carries the push version
   the page starts from, so the first patch can be checked against something.
2. **After that, a fold pushes only the blocks that subscribe to it.** One crew-log bus
   subscription per fold the Model names; a fold that moves is turned into the values of
   the blocks whose fields read THAT fold, and nothing else is sent.
3. **Over the existing WebSocket.** ``state.broadcast_ws_owners`` -- the same hub and
   the same owner-socket set ``slot_projection`` uses. :data:`BLOCK_FRAME` is a new
   message TYPE on that channel, deliberately not a second channel: a socket of its own
   would be a second place to get the owner gate, the backpressure and the reconnect
   right.
4. **A version gap, or a layout change, triggers a full refetch.** Every patch carries
   ``version`` -- a counter that steps by exactly one per frame for this page -- and
   ``layout``, the package artifact's version. The page refetches when ``version`` is
   not the one it holds plus one, which also covers a gateway restart (the counter
   restarts at zero, which is not ``held + 1``). A layout change is detected HERE, on
   the re-read below, and sent as a frame carrying ``refetch`` and no values at all:
   the block set itself may have changed, so values computed under the new layout must
   not be applied to a document rendered under the old one.
5. **The owner check and the redaction stay in the controller.** Neither is implemented
   in this module. ``redact`` and ``reread`` are handed in by
   :func:`~kiro_crew.dashboard.handlers.member_dashboard.arm_block_push`, which is where
   the binding is checked and where ``_page_safe`` lives; this module cannot emit a
   value that did not cross ``redact`` because :meth:`LivePage.blocks` is the only place
   values are produced and it calls it on every path.

Threads. A bus callback runs on the crew log's FOLD WORKER thread, synchronously inside
``bus.publish``, so it does what every other subscriber in this repo does: compare a
revision, store a value, hop to the loop, return. Nothing else -- no IO, no socket, no
lock held across the hop. The re-read that decides a layout change is file IO and runs
on a worker thread through ``asyncio.to_thread``, never on the loop and never on the
folder.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Final, Mapping

from kiro_crew.crew_log.projection import DASHBOARD_FOLD_NAME as _DASHBOARD_FOLD_NAME
from kiro_crew.dashboard_feed import MISSING, resolve_path, scope_for

logger = logging.getLogger(__name__)

__all__ = [
    "BLOCK_FRAME",
    "LivePage",
    "close_all",
    "close_page",
    "live_page",
    "open_page",
]

#: The one WS message type this push uses. A TYPE on the existing owner channel, not a
#: channel: see the module docstring.
BLOCK_FRAME: Final[str] = "dashboard_block_patch"

#: The fold a package's agentic fields are read from. The crewmate's own writes land in
#: one slot fold, exactly as ``member_dashboard.read_fields`` reads them for a v2 page.
#:
#: Read from the projection kernel rather than spelled here, because the kernel's own
#: name for it (``entry_types.DASHBOARD_FOLD_NAME``) is ``"agentic"`` -- two spellings of
#: one fold is how a push ends up subscribed to a fold nothing ever advances.
AGENTIC_FOLD: Final[str] = _DASHBOARD_FOLD_NAME

#: How many crewmates may have a live push at once. Each page holds one bus subscription
#: per fold it reads, so an unbounded map would grow for the life of the gateway on a
#: host whose operator opens every member's tab. The least recently armed page is closed
#: when the cap is passed, which costs that tab its pushes until its next read arms it
#: again -- a refetch, never a wrong value.
MAX_LIVE_PAGES: Final[int] = 32

#: Why a frame asks for a full refetch. Sent as ``reason`` beside ``refetch: true``, so
#: a reader looking at one frame can tell a recomposed layout from a rebound page.
REASON_LAYOUT: Final[str] = "layout_changed"
REASON_UNBOUND: Final[str] = "package_unbound"


@dataclass
class _Cell:
    """One fold's latest value and the revision that orders it.

    ``revision`` is what orders two values of one fold and ``seq`` is not -- the crew-log
    bus documents why, and :mod:`kiro_crew.dashboard_feed` repeats it: a slot fold's seq
    is the newest unit's, so it can move DOWN while the value moves forward.
    """

    value: Mapping[str, Any] | None = None
    revision: int = 0
    seq: int = 0
    seen: bool = False


class LivePage:
    """One crewmate's open v3 dashboard.

    Built by :func:`open_page` and never directly: the registry is what makes a second
    arming of the same crewmate refresh one page rather than leave two sets of
    subscriptions writing to two counters the browser cannot tell apart.
    """

    def __init__(
        self,
        slug: str,
        member: str,
        model: Any,
        *,
        state: Any,
        loop: asyncio.AbstractEventLoop | None,
        redact: Callable[[Any], Any],
        reread: Callable[[str], Any],
    ) -> None:
        if not callable(redact):
            # Refused rather than defaulted. A default would be a second redactor, and
            # the one that matters is the controller's.
            raise TypeError("a live dashboard page needs the controller's redactor")
        self.slug = slug
        self.member = member
        self.model = model
        self._state = state
        self._loop = loop
        self._redact = redact
        self._reread = reread
        self._lock = threading.Lock()
        self._cells: dict[str, _Cell] = {}
        self._disposers: list[Callable[[], None]] = []
        self._version = 0
        self._closed = False

    # -- identity ----------------------------------------------------------- #

    @property
    def version(self) -> int:
        """The push version the browser should be holding. Carried by the first load."""
        return self._version

    @property
    def layout(self) -> int:
        """The package artifact's version, which moves only on a layout change."""
        return int(getattr(self.model, "version", 0) or 0)

    @property
    def binding(self) -> str:
        return str(getattr(self.model, "bound_to", "") or "")

    def bind(self, state: Any, loop: asyncio.AbstractEventLoop | None) -> None:
        """Re-point this page at the loop and the hub now serving.

        The same reason ``install_crew_log_publisher`` re-binds its publisher: a gateway
        restarted inside one interpreter must push on the loop that is actually serving,
        through the hub that actually holds the sockets.
        """
        self._state = state
        self._loop = loop

    # -- subscription ------------------------------------------------------- #

    def folds(self) -> frozenset[str]:
        """The folds this page reads: the Model's, plus the agentic one when it writes."""
        folds = set(getattr(self.model, "folds", frozenset()))
        if getattr(self.model, "agentic_fields", None) and self.model.agentic_fields():
            folds.add(AGENTIC_FOLD)
        return frozenset(folds)

    def bus_key(self, scope: str) -> str:
        """The bus key this page subscribes with under *scope*, or ``""`` to skip it.

        THE ONE PLACE A KEY KIND IS DECIDED, and the extension point for a third one.
        ``dashboard_feed.scope_for`` answers which scope a fold is keyed by, off the
        projection kernel's own sets; this answers what the key then IS, which only
        this page knows.

        Today exactly one kind is reachable. A package's binding is ``crewmate:<slug>``
        or ``session:<slot key>``, so a page always has a SLOT key -- the crewmate's own
        slug -- and never a session UNIT id, which is what ``SCOPE_SESSION`` is keyed by.
        A session fold is therefore not something this page can subscribe to at all, and
        saying so is better than guessing a key and never receiving an event.

        A THIRD KEY KIND (D4's registered tree fold) lands here: give its scope a row
        and the subscription path above takes it unchanged. Until it has one, a fold
        under it is reported unavailable rather than silently never delivered -- which
        is the difference this method exists to make.
        """
        from kiro_crew.crew_log import bus as crew_log_bus

        if scope == crew_log_bus.SCOPE_SLOT:
            return self.slug
        return ""

    def subscribe(self) -> list[str]:
        """Subscribe to this page's folds; return the ones that could not be.

        ``baseline=True`` on every subscription, for the reason :class:`DashboardFeed`
        gives: without it the page holds a subscription that is correct from now on and
        empty until the crewmate happens to do something, so a dashboard opened on a
        quiet crewmate would push nothing over a log that has the numbers in it. THE
        BASELINE READ IS FILE IO on the calling thread, so a caller on the event loop
        runs this through ``asyncio.to_thread``.
        """
        from kiro_crew.crew_log import bus as crew_log_bus

        self.unsubscribe()
        unavailable: list[str] = []
        for fold in sorted(self.folds()):
            scope = scope_for(fold)
            key = self.bus_key(scope)
            if not scope or not key:
                # A fold this page cannot reach. Reported rather than raised -- the
                # fields it feeds read as missing, which is the honest rendering, and
                # one unreachable fold must not deny the page the others.
                unavailable.append(fold)
                continue
            with self._lock:
                self._cells.setdefault(fold, _Cell())
            try:
                self._disposers.append(
                    crew_log_bus.subscribe(
                        crew_log_bus.FOLD_ADVANCED,
                        self._callback(fold),
                        scope=scope,
                        key=key,
                        fold=fold,
                        baseline=True,
                    )
                )
            except Exception:
                logger.warning(
                    "dashboard push: the %s fold could not be subscribed for %r",
                    fold,
                    self.slug,
                    exc_info=True,
                )
                unavailable.append(fold)
        return unavailable

    def ensure_subscribed(self) -> list[str]:
        """Subscribe only if this page is not subscribed already.

        What the READ calls, because a reused page is the ordinary case: a tab that
        polls would otherwise tear down its subscriptions and re-run a baseline file
        read per fold per request, and -- worse -- serve its full load from a cache it
        had just emptied. A page that is already subscribed holds values the bus has
        been keeping current, which is the whole point of a live feed.

        Retried when nothing is subscribed, which includes the case where every fold
        was unavailable last time: the condition may have changed.
        """
        if self._disposers:
            return []
        return self.subscribe()

    def unsubscribe(self) -> None:
        """Drop every subscription and forget every cached value. Idempotent."""
        disposers, self._disposers = self._disposers, []
        for dispose in disposers:
            dispose()
        with self._lock:
            self._cells.clear()

    def close(self) -> None:
        """Stop pushing for good. A closed page never sends another frame."""
        self._closed = True
        self.unsubscribe()

    def _callback(self, fold: str) -> Callable[[Any], None]:
        """The per-fold callback, closed over the fold NAME rather than reading it.

        The bus filters on fold already, so the event's own name adds nothing -- and
        taking it from the closure means a malformed event cannot write into the wrong
        cell, which is the one thing a cache must not let a publisher do.
        """

        def _apply(event: Any) -> None:
            self.on_fold(fold, event)

        return _apply

    # -- fold worker thread -------------------------------------------------- #

    def on_fold(self, fold: str, event: Any) -> None:
        """Store one fold value if it is newer, then hand the push to the loop.

        Runs on the FOLD WORKER thread. The event is read DEFENSIVELY -- a bus carries
        whatever a publisher sends, and a malformed one must cost this push rather than
        the fan-out to the next subscriber.
        """
        if self._closed:
            return
        revision = int(getattr(event, "revision", 0) or 0)
        seq = int(getattr(event, "seq", 0) or 0)
        value = getattr(event, "value", None)
        if revision <= 0 or not isinstance(value, Mapping):
            return
        if fold not in self.folds():
            # A fold this page does not read. The bus filters on fold already and the
            # callback is closed over the name, so this cannot happen through a
            # subscription -- it is the guard for a caller holding an event, and it is
            # the one thing a cache must not let a publisher do: write the wrong cell.
            return
        with self._lock:
            cell = self._cells.get(fold)
            if cell is None:
                # Created on first arrival rather than only by :meth:`subscribe`, so a
                # value cannot be dropped for arriving between the subscribe and the
                # baseline -- the fold set above is what decides, not the cache's shape.
                cell = self._cells[fold] = _Cell()
            if cell.seen and revision <= cell.revision:
                # ORDERED BY REVISION, never by arrival: a baseline and a held event can
                # reach here in either order and seq cannot order them.
                return
            cell.value = value
            cell.revision = revision
            cell.seq = seq
            cell.seen = True
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._arm, fold)
        except RuntimeError:
            # The loop is closed, which happens while the gateway shuts down. A push
            # nobody can receive is not worth reporting.
            logger.debug("dashboard push for %r/%s arrived after the loop closed", self.slug, fold)

    # -- event loop ---------------------------------------------------------- #

    def _arm(self, fold: str) -> None:
        """Start the send for *fold*. On the loop."""
        loop = self._loop
        if loop is None or self._closed:
            return
        task = loop.create_task(self.send(fold))
        task.add_done_callback(_report_failure)

    async def send(self, fold: str) -> bool:
        """Send one frame for *fold*. True when a frame went out. On the loop.

        The ORDER of the three decisions is the protocol: is this page still the
        crewmate's at all, has its layout moved, and only then which blocks heard the
        fold. Deciding the blocks first would compute values under a Model the stored
        package has already replaced.
        """
        if self._closed:
            return False
        fresh = await asyncio.to_thread(self._reread, self.slug)
        if fresh is None or str(getattr(fresh, "bound_to", "")) != self.binding:
            # The package is gone, or bound somewhere else now. A rebind writes no
            # version (the package line's ruling), so it is invisible to the layout
            # check and has to be caught here. The frame carries NO values: a page that
            # is no longer this crewmate's must not be told one more number.
            self.close()
            return self._refetch(REASON_UNBOUND)
        if int(getattr(fresh, "version", 0) or 0) != self.layout or str(
            getattr(fresh, "layout_fingerprint", "")
        ) != str(self.model.layout_fingerprint):
            self.model = fresh
            return self._refetch(REASON_LAYOUT)
        blocks, missing = self.blocks(folds=frozenset({fold}))
        if not blocks:
            # NOTHING SUBSCRIBES, so nothing is sent and the counter does not move. A
            # frame here would spend a version on a page that has nothing to apply.
            return False
        self._version += 1
        self._broadcast(
            {
                "slug": self.slug,
                "dashboard": str(getattr(self.model, "slug", "") or ""),
                "version": self._version,
                "layout": self.layout,
                "fold": fold,
                "blocks": blocks,
                "missing": missing,
                "refetch": False,
                "reason": "",
            }
        )
        return True

    def _refetch(self, reason: str) -> bool:
        """Ask the page to read the whole thing again. Carries no values, by design."""
        self._version += 1
        self._broadcast(
            {
                "slug": self.slug,
                "dashboard": str(getattr(self.model, "slug", "") or ""),
                "version": self._version,
                "layout": self.layout,
                "fold": "",
                "blocks": {},
                "missing": [],
                "refetch": True,
                "reason": reason,
            }
        )
        return True

    def _broadcast(self, data: dict[str, Any]) -> None:
        """Hand one frame to the hub. Never raises: a push must not cost the loop."""
        broadcast = getattr(self._state, "broadcast_ws_owners", None)
        if broadcast is None:
            return
        try:
            broadcast(BLOCK_FRAME, data)
        except Exception:
            logger.debug("dashboard push for %r failed", self.slug, exc_info=True)

    # -- values -------------------------------------------------------------- #

    def blocks(
        self, *, folds: frozenset[str] | None = None
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """``(blocks, missing)`` for this page, redacted. The ONE place values come from.

        *folds* ``None`` is the whole page, which is what the first load serves. A set
        selects only the blocks that read one of those folds -- :meth:`DashboardModel.
        block_folds` is that lookup, and a block reading none of them is absent from the
        result rather than present and empty.

        A field whose path does not resolve is NAMED in ``missing`` instead of carried
        as a value: the page dims that cell, and a ``None`` would render as a zero.

        Every value crosses the controller's redactor on the way out, in ONE call over
        the whole mapping rather than per field -- the redactor is recursive and covers
        keys as well, and a per-field call would be a per-field chance to forget one.
        """
        model = self.model
        with self._lock:
            cells = {name: (cell.value, cell.seen) for name, cell in self._cells.items()}
        out: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for block_id in getattr(model, "subscriptions", {}):
            if folds is not None and not (model.block_folds(block_id) & folds):
                continue
            values: dict[str, Any] = {}
            for name in model.subscriptions[block_id]:
                resolved = self._value_of(name, cells)
                if resolved is MISSING:
                    if name not in missing:
                        missing.append(name)
                    continue
                values[name] = resolved
            out[block_id] = values
        return self._redact(out), missing

    def _value_of(self, name: str, cells: Mapping[str, tuple[Any, bool]]) -> Any:
        """One field's value from the cached folds, or :data:`MISSING`.

        An agentic field is read from the agentic fold's own row, which is where the
        crewmate's write landed and where its ``at`` stamp lives. A fold-backed field
        walks its declared path into its fold's value. The two cannot be confused: the
        Model's ``source`` says which, and it is required on every field.
        """
        spec = self.model.fields.get(name)
        if not isinstance(spec, Mapping):
            return MISSING
        source = spec.get("source")
        if not isinstance(source, Mapping):
            return MISSING
        if source.get("agentic") is True:
            cached = cells.get(AGENTIC_FOLD)
            if cached is None or not cached[1]:
                return MISSING
            row = resolve_path(cached[0], f"fields.{name}")
            if not isinstance(row, Mapping) or "value" not in row:
                return MISSING
            return row["value"]
        fold = str(source.get("fold") or "")
        cached = cells.get(fold)
        if cached is None or not cached[1]:
            return MISSING
        return resolve_path(cached[0], str(source.get("path") or ""))


def _report_failure(task: "asyncio.Task[Any]") -> None:
    """Log a push task that raised. Without this the exception is only ever a warning
    from the loop's default handler, with no slug in it."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("dashboard push task failed", exc_info=exc)


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #

#: The live pages, oldest first. LRU by arming, bounded at :data:`MAX_LIVE_PAGES`.
_PAGES: "OrderedDict[str, LivePage]" = OrderedDict()
_PAGES_LOCK = threading.Lock()


def live_page(slug: str) -> LivePage | None:
    """The live page for *slug*, or ``None``. For the controller and a test."""
    with _PAGES_LOCK:
        return _PAGES.get(slug)


def open_page(
    slug: str,
    member: str,
    model: Any,
    *,
    state: Any,
    loop: asyncio.AbstractEventLoop | None,
    redact: Callable[[Any], Any],
    reread: Callable[[str], Any],
) -> LivePage:
    """Arm -- or refresh -- the live push for one crewmate's package page.

    IDEMPOTENT for an unchanged page: a second read of the same dashboard re-points the
    existing page at the loop and hub now serving and returns it, rather than building a
    second set of subscriptions. Two sets would write to two counters, and the browser
    cannot tell two senders apart -- it would see the versions interleave and refetch on
    every frame.

    REPLACED when the member, the binding or the layout differ, because then the blocks
    this page would push are not the blocks the document on screen is holding.
    """
    with _PAGES_LOCK:
        existing = _PAGES.get(slug)
        reusable = (
            existing is not None
            and not existing._closed
            and existing.member == member
            and existing.binding == str(getattr(model, "bound_to", "") or "")
            and existing.layout == int(getattr(model, "version", 0) or 0)
            and existing.model.layout_fingerprint == model.layout_fingerprint
        )
        if reusable and existing is not None:
            existing.bind(state, loop)
            _PAGES.move_to_end(slug)
            return existing
        page = LivePage(slug, member, model, state=state, loop=loop, redact=redact, reread=reread)
        if existing is not None:
            existing.close()
        _PAGES[slug] = page
        _PAGES.move_to_end(slug)
        evicted: list[LivePage] = []
        while len(_PAGES) > MAX_LIVE_PAGES:
            _slug, old = _PAGES.popitem(last=False)
            evicted.append(old)
    for old in evicted:
        old.close()
    return page


def close_page(slug: str) -> None:
    """Stop pushing for *slug*. Idempotent."""
    with _PAGES_LOCK:
        page = _PAGES.pop(slug, None)
    if page is not None:
        page.close()


def close_all() -> None:
    """Stop pushing for every page. For a gateway shutdown and for a test's teardown."""
    with _PAGES_LOCK:
        pages = list(_PAGES.values())
        _PAGES.clear()
    for page in pages:
        page.close()
