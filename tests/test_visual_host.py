"""Tests for visual hosting ("portals").

A widget keeps its *logical* parent (used for queries, CSS selector matching,
event bubbling and unmount / worker bookkeeping) while its layout, composition
and mouse hit testing move to a separate *visual* host.
"""

from __future__ import annotations

import asyncio

import pytest

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.lazy import Lazy
from textual.message import Message
from textual.widget import WidgetError
from textual.widgets import Label
from textual.worker import WorkerState


CSS = """
Screen { layout: horizontal; }
#toolbar { width: 20; height: 24; }
.host { width: 30; height: 24; border: solid green; }
#p { width: 8; height: 2; offset: 1 1; color: auto; }
#b { width: 6; height: 1; offset: 1 1; color: auto; }
/* The portal can only be reached through its *logical* ancestors. */
#toolbar #p { color: red; }
/* This must never match: #host is not the portal's logical parent. */
#host1 #p { color: blue; }
"""


class PortalPing(Message):
    """A message that bubbles up the logical tree."""

    bubble = True


class Hosted(Label):
    pass


class Inner(Label):
    pass


class Host(Container):
    def __init__(self, **kwargs):
        self.pings = 0
        super().__init__(**kwargs)

    def on_portal_ping(self) -> None:
        self.pings += 1


class Toolbar(Horizontal):
    def __init__(self, **kwargs):
        self.pings = 0
        super().__init__(**kwargs)

    def on_portal_ping(self) -> None:
        self.pings += 1


async def _long_job() -> None:
    await asyncio.sleep(1000)


class PortalApp(App[None]):
    CSS = CSS

    start_worker: bool = False
    attach: bool = True

    def compose(self) -> ComposeResult:
        with Toolbar(id="toolbar"):
            yield Hosted("P", id="p")
            yield Label("x", id="tlabel")
        with Host(id="host1", classes="host"):
            yield Label("1")
        with Host(id="host2", classes="host"):
            yield Label("2")

    def on_mount(self) -> None:
        portal = self.query_one("#p", Hosted)
        if self.start_worker:
            self.portal_worker = portal.run_worker(_long_job())
        if self.attach:
            portal.attach_visual_host(self.query_one("#host1", Host))

    def on_click(self, event) -> None:
        if isinstance(event.control, Hosted):
            event.control.post_message(PortalPing())


# --------------------------------------------------------------------------- #
# Relationships, queries and selectors
# --------------------------------------------------------------------------- #


async def test_default_has_no_visual_host():
    app = PortalApp()
    app.attach = False
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        assert portal.visual_host is None
        assert portal.visual_children == ()


async def test_attach_keeps_logical_parent_and_registers_visual_child():
    app = PortalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        toolbar = app.query_one("#toolbar", Toolbar)
        host = app.query_one("#host1", Host)
        assert portal.parent is toolbar
        assert portal.visual_host is host
        assert portal in host.visual_children


async def test_logical_query_and_selector_still_find_portal():
    app = PortalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        toolbar = app.query_one("#toolbar", Toolbar)
        portal = app.query_one("#p", Hosted)
        # Logical parent query walks the logical tree.
        assert toolbar.query_one("#p") is portal
        # A selector using the logical ancestor path matches.
        assert app.query_one("Horizontal #p") is portal
        # walk_children is also logical, so the portal is found under toolbar...
        assert portal in toolbar.walk_children(Hosted)
        # ...and is not enumerated as a DOM child of the visual host.
        assert portal not in host_walk(app)


def host_walk(app: PortalApp):
    return app.query_one("#host1", Host).walk_children(Hosted)


async def test_css_matches_through_logical_ancestors_only():
    app = PortalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        # `#toolbar #p` matches (logical); `#host1 #p` does not (visual only),
        # so the colour is red, not the later blue rule.
        assert portal.styles.color.rgb == (255, 0, 0)


# --------------------------------------------------------------------------- #
# Layout / composition / hit testing
# --------------------------------------------------------------------------- #


async def test_layout_is_relative_to_visual_host():
    app = PortalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        toolbar = app.query_one("#toolbar", Toolbar)
        host = app.query_one("#host1", Host)
        region = portal.region
        # offset (1,1) is applied relative to the *host's* content box.
        content = host.content_region
        assert region.x == content.x + 1
        assert region.y == content.y + 1
        # The portal no longer occupies its (narrow) logical parent.
        assert region.x >= toolbar.region.right


async def test_portal_escapes_logical_parent_clipping():
    app = PortalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        toolbar = app.query_one("#toolbar", Toolbar)
        # A point inside the rendered portal is well outside the toolbar, yet
        # still resolves to the portal (it is clipped to the screen, not to the
        # logical parent).
        point = portal.region.offset + (1, 1)
        assert not toolbar.region.contains(*point)
        hit, _ = app.get_widget_at(*point)
        assert hit is portal


async def test_hit_testing_uses_visual_location():
    app = PortalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        # Visual point -> portal.
        x, y = portal.region.offset + (1, 1)
        hit, _ = app.get_widget_at(x, y)
        assert hit is portal
        # The toolbar area where the portal *would* have been no longer hits it.
        hit_old, _ = app.get_widget_at(1, 1)
        assert hit_old is not portal


async def test_pilot_clicks_visual_location_via_logical_selector():
    app = PortalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        toolbar = app.query_one("#toolbar", Toolbar)
        host = app.query_one("#host1", Host)
        # Selector is resolved through the logical tree; the click lands at the
        # visual location.
        assert await pilot.click("#p", offset=(1, 1)) is True
        await pilot.pause()
        assert toolbar.pings == 1
        assert host.pings == 0


async def test_bubble_follows_logical_parent_not_visual_host():
    app = PortalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        toolbar = app.query_one("#toolbar", Toolbar)
        host = app.query_one("#host1", Host)
        portal.post_message(PortalPing())
        await pilot.pause()
        assert toolbar.pings == 1
        assert host.pings == 0


# --------------------------------------------------------------------------- #
# Detach / replace
# --------------------------------------------------------------------------- #


async def test_detach_restores_logical_layout():
    app = PortalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        toolbar = app.query_one("#toolbar", Toolbar)
        host = app.query_one("#host1", Host)
        portal.detach_visual_host()
        await pilot.pause()
        assert portal.visual_host is None
        assert host.visual_children == ()
        assert portal.region.x < toolbar.region.right
        assert portal.region.x < host.region.x


async def test_replace_host_moves_portal_without_cancelling_worker():
    app = PortalApp()
    app.start_worker = True
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        host1 = app.query_one("#host1", Host)
        host2 = app.query_one("#host2", Host)
        worker = app.portal_worker
        assert worker.state == WorkerState.RUNNING

        portal.attach_visual_host(host2)
        await pilot.pause()
        assert portal.visual_host is host2
        assert portal not in host1.visual_children
        assert portal in host2.visual_children
        assert portal.region.x >= host2.region.x
        # Replacing the visual host is purely visual: no unmount, no cancel.
        assert portal.is_attached
        assert worker.state == WorkerState.RUNNING


# --------------------------------------------------------------------------- #
# Unmount coupling / decoupling
# --------------------------------------------------------------------------- #


async def test_removing_visual_host_keeps_logical_node_and_worker():
    app = PortalApp()
    app.start_worker = True
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        host = app.query_one("#host1", Host)
        worker = app.portal_worker

        await host.remove()
        await pilot.pause()
        # The portal stays on the logical tree, its task keeps running, and it
        # falls back to being laid out by its logical parent.
        assert portal.is_attached
        assert portal.visual_host is None
        assert worker.state == WorkerState.RUNNING
        toolbar = app.query_one("#toolbar", Toolbar)
        assert portal.region.x < toolbar.region.right


async def test_removing_logical_parent_tears_down_hosting_and_cancels_worker():
    app = PortalApp()
    app.start_worker = True
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        toolbar = app.query_one("#toolbar", Toolbar)
        host = app.query_one("#host1", Host)
        worker = app.portal_worker

        await toolbar.remove()
        await pilot.pause()
        await pilot.pause()
        assert not portal.is_attached
        # Hosting relationship is torn down on logical unmount...
        assert portal not in host.visual_children
        assert host.visual_children == ()
        # ...and the node's background task is cancelled by the normal rule.
        assert worker.state in (WorkerState.CANCELLED, WorkerState.ERROR)


# --------------------------------------------------------------------------- #
# Nested hosting
# --------------------------------------------------------------------------- #


async def test_nested_hosting_is_relative_and_restores():
    app = PortalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        host1 = app.query_one("#host1", Host)
        host2 = app.query_one("#host2", Host)

        await portal.mount(Inner("B", id="b"))
        await pilot.pause()
        inner = app.query_one("#b", Inner)

        # Outer portal already lives in host1; nest the inner widget in host2.
        inner.attach_visual_host(host2)
        await pilot.pause()
        assert inner.parent is portal
        assert inner.visual_host is host2
        inner_content = host2.content_region
        assert inner.region.x == inner_content.x + 1
        assert inner.region.y == inner_content.y + 1

        # Detaching the inner relationship restores it inside the (still
        # hosted) outer portal.
        inner.detach_visual_host()
        await pilot.pause()
        assert inner.visual_host is None
        assert inner.region.x >= portal.region.x
        assert inner.region.y >= portal.region.y
        # The outer relationship is untouched.
        assert portal.visual_host is host1


# --------------------------------------------------------------------------- #
# Lazy (delayed mount) combination
# --------------------------------------------------------------------------- #


class _SelfHosting(Label):
    def on_mount(self) -> None:
        self.attach_visual_host(self.app.query_one("#host1", Host))


LAZY_CSS = CSS


class DirectApp(App[None]):
    CSS = LAZY_CSS

    def compose(self) -> ComposeResult:
        with Toolbar(id="toolbar"):
            yield _SelfHosting("P", id="p")
        with Host(id="host1", classes="host"):
            yield Label("1")
        with Host(id="host2", classes="host"):
            yield Label("2")


class LazyHostApp(App[None]):
    CSS = LAZY_CSS

    def compose(self) -> ComposeResult:
        with Toolbar(id="toolbar"):
            yield Lazy(_SelfHosting("P", id="p"))
        with Host(id="host1", classes="host"):
            yield Label("1")
        with Host(id="host2", classes="host"):
            yield Label("2")


async def _probe(app: App):
    async with app.run_test() as pilot:
        for _ in range(6):
            await pilot.pause()
        portal = app.query_one("#p")
        toolbar = app.query_one("#toolbar", Toolbar)
        host = app.query_one("#host1", Host)
        assert portal.parent is toolbar
        assert portal.visual_host is host
        point = portal.region.offset + (1, 1)
        hit, _ = app.get_widget_at(*point)
        assert hit is portal
        assert await pilot.click("#p", offset=(1, 1)) is True
        return portal.region


async def test_lazy_attach_is_equivalent_to_direct_attach():
    direct_region = await _probe(DirectApp())
    lazy_region = await _probe(LazyHostApp())
    assert direct_region == lazy_region


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


async def test_cannot_host_self():
    app = PortalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        with pytest.raises(WidgetError):
            portal.attach_visual_host(portal)


async def test_cannot_host_logical_descendant():
    app = PortalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        toolbar = app.query_one("#toolbar", Toolbar)
        portal = app.query_one("#p", Hosted)
        # toolbar is an ancestor of portal, so portal can't visually host it
        # (that would create a visual cycle).
        with pytest.raises(WidgetError):
            toolbar.attach_visual_host(portal)


async def test_cannot_attach_before_mount():
    portal = Hosted("P")
    host = Host()
    with pytest.raises(WidgetError):
        portal.attach_visual_host(host)


async def test_screen_can_be_visual_host():
    """A toolbar control can portal to the screen layer to escape clipping."""
    app = PortalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        portal = app.query_one("#p", Hosted)
        toolbar = app.query_one("#toolbar", Toolbar)
        portal.detach_visual_host()
        await pilot.pause()

        # Re-host directly on the screen.
        portal.attach_visual_host(app.screen)
        await pilot.pause()
        assert portal.visual_host is app.screen
        assert portal.parent is toolbar
        # Positioned relative to the screen's content box, painted over it.
        region = portal.region
        assert region.offset == app.screen.content_region.offset + (1, 1)
        hit, _ = app.get_widget_at(*(region.offset + (1, 1)))
        assert hit is portal

