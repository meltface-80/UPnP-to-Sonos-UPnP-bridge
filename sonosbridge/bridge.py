"""Wiring: discovery, virtual renderers, SSDP, HTTP and event plumbing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import aiohttp
from aiohttp import web

from .config import BRIDGE_NAME, BRIDGE_VERSION, Config
from .discovery import TopologyManager
from .gena import SonosSubscription, SubscriptionManager
from .net import local_ip_towards
from .renderer import VirtualRenderer
from .server import create_app
from .soap import SoapClient
from .sonos import AV_TRANSPORT, RENDERING_CONTROL, SonosPlayer, ZoneInfo
from .ssdp import SsdpServer

LOGGER = logging.getLogger(__name__)

# Renew a Sonos subscription well before it lapses; the player drops us silently
# if a renewal is late.
RENEW_SAFETY_FACTOR = 0.5
RENEW_CHECK_INTERVAL = 30.0


class SonosEventLink:
    """Holds the two GENA subscriptions the bridge keeps on a Sonos player."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        renderer: VirtualRenderer,
        callback_base: str,
        timeout: int,
    ) -> None:
        self._session = session
        self.renderer = renderer
        self._callback_base = callback_base.rstrip("/")
        self._timeout = timeout
        self._subs: dict[str, SonosSubscription] = {}
        self._targets: dict[str, str] = {}

    def _service_player(self, service_type: str) -> SonosPlayer:
        # Transport state lives with the group coordinator; volume is per room.
        if service_type == AV_TRANSPORT:
            return self.renderer.coordinator()
        return self.renderer.player()

    async def ensure(self) -> None:
        """(Re)subscribe wherever a subscription is missing, stale, or now points
        at the wrong player because the room was grouped or ungrouped."""
        for service_type, name in ((AV_TRANSPORT, "AVTransport"),
                                   (RENDERING_CONTROL, "RenderingControl")):
            player = self._service_player(service_type)
            event_url = player.event_url(service_type)
            existing = self._subs.get(name)

            if existing is not None and self._targets.get(name) != event_url:
                await existing.unsubscribe()
                existing = None

            if existing is None:
                existing = SonosSubscription(
                    self._session,
                    event_url,
                    f"{self._callback_base}/sonos/{self.renderer.uuid}/{name}",
                    self._timeout,
                )
                self._subs[name] = existing
                self._targets[name] = event_url
                if await existing.subscribe():
                    LOGGER.debug("%s: subscribed to %s", self.renderer.zone.name, event_url)
                else:
                    LOGGER.debug(
                        "%s: could not subscribe to %s; falling back to polling",
                        self.renderer.zone.name,
                        event_url,
                    )
                continue

            remaining = existing.expires_at - time.monotonic()
            if remaining < self._timeout * RENEW_SAFETY_FACTOR:
                await existing.renew()

        self.renderer.sonos_sids = {sub.sid for sub in self._subs.values() if sub.sid}

    async def close(self) -> None:
        for subscription in self._subs.values():
            await subscription.unsubscribe()
        self._subs.clear()
        self._targets.clear()
        self.renderer.sonos_sids = set()


class Bridge:
    """The whole application, assembled."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.bridge_ip = config.bridge_ip or local_ip_towards()
        self.base_url = f"http://{self.bridge_ip}:{config.http_port}"
        self.renderers: dict[str, VirtualRenderer] = {}
        self._by_zone: dict[str, VirtualRenderer] = {}
        self._links: dict[str, SonosEventLink] = {}
        self._session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._ssdp: SsdpServer | None = None
        self._topology: TopologyManager | None = None
        self._tasks: list[asyncio.Task] = []
        self._started_at = time.time()

    # ------------------------------------------------------------------
    def renderer_for(self, uuid: str) -> VirtualRenderer | None:
        return self.renderers.get(uuid)

    def status(self) -> dict[str, object]:
        return {
            "name": BRIDGE_NAME,
            "version": BRIDGE_VERSION,
            "bridgeIp": self.bridge_ip,
            "httpPort": self.config.http_port,
            "mode": self.config.mode,
            "uptimeSeconds": int(time.time() - self._started_at),
            "devices": [r.status() for r in self.renderers.values()],
        }

    # ------------------------------------------------------------------
    async def start(self) -> None:
        connector = aiohttp.TCPConnector(limit=64, force_close=True, enable_cleanup_closed=True)
        self._session = aiohttp.ClientSession(connector=connector)
        soap_client = SoapClient(self._session, self.config.http_timeout)

        self._topology = TopologyManager(
            self.config, self._session, soap_client, self.bridge_ip
        )
        self._topology.set_callback(self._on_zones_changed)

        app = create_app(self)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.config.http_port)
        await site.start()
        LOGGER.info("HTTP server listening on %s", self.base_url)

        self._ssdp = SsdpServer(
            self.bridge_ip,
            lambda: list(self.renderers.values()),
            max_age=self.config.ssdp_max_age,
            alive_interval=self.config.ssdp_alive_interval,
            ttl=self.config.multicast_ttl,
            port=self.config.ssdp_port,
            boot_id=int(self._started_at) & 0x7FFFFFFF,
            on_sonos_seen=self._topology.note_host,
        )
        try:
            await self._ssdp.start()
        except OSError as exc:
            # Without SSDP nothing will discover the bridge, but staying up means
            # the status page can still explain what went wrong.
            LOGGER.error(
                "Could not open the SSDP socket (%s). Control points will not "
                "find the bridge. Run the container with --network host, and "
                "check nothing else on this host is bound to UDP %d.",
                exc,
                self.config.ssdp_port,
            )
            self._ssdp = None

        await self._topology.discover()
        await self._topology.refresh()

        self._tasks = [
            asyncio.create_task(self._discovery_loop(), name="discovery"),
            asyncio.create_task(self._topology_loop(), name="topology"),
            asyncio.create_task(self._subscription_loop(), name="subscriptions"),
            asyncio.create_task(self._poll_loop(), name="poll"),
        ]

        if not self.renderers:
            LOGGER.warning(
                "No Sonos rooms bridged yet - discovery continues in the "
                "background. See %s for status.",
                self.base_url,
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        for link in self._links.values():
            with contextlib.suppress(Exception):
                await link.close()
        self._links.clear()

        if self._ssdp:
            await self._ssdp.stop()
            self._ssdp = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._session:
            await self._session.close()
            self._session = None
        LOGGER.info("Bridge stopped")

    # ------------------------------------------------------------------
    async def _on_zones_changed(self, added: list[ZoneInfo], removed: list[str]) -> None:
        for uid in removed:
            renderer = self._by_zone.pop(uid, None)
            if renderer is None:
                continue
            LOGGER.info("Sonos room gone: %s", renderer.zone.name)
            if self._ssdp:
                self._ssdp.announce_byebye(renderer)
            self.renderers.pop(renderer.uuid, None)
            link = self._links.pop(renderer.uuid, None)
            if link:
                with contextlib.suppress(Exception):
                    await link.close()

        for zone in added:
            renderer = VirtualRenderer(
                self.config, zone, self._topology, self._soap_client(), self.base_url
            )
            renderer.subscriptions = SubscriptionManager(
                self._session, self.config.event_timeout
            )
            self.renderers[renderer.uuid] = renderer
            self._by_zone[zone.uid] = renderer
            LOGGER.info(
                "Bridging %s -> %s (%s)", zone.name, renderer.friendly_name, zone.ip
            )
            if self._ssdp:
                # Announce twice: SSDP is UDP and the first packet is often lost.
                self._ssdp.announce_alive(renderer)
                asyncio.get_running_loop().call_later(
                    1.0, self._ssdp.announce_alive, renderer
                )
            link = SonosEventLink(
                self._session, renderer, self.base_url, self.config.sonos_sub_timeout
            )
            self._links[renderer.uuid] = link
            await link.ensure()
            await self._prime(renderer)

    def _soap_client(self) -> SoapClient:
        return SoapClient(self._session, self.config.http_timeout)

    async def _prime(self, renderer: VirtualRenderer) -> None:
        """Fill in volume/transport state so the first event is not empty."""
        with contextlib.suppress(Exception):
            await renderer.refresh()
        with contextlib.suppress(Exception):
            await renderer.sink_protocol_info()

    def _sync_zone_data(self) -> None:
        """Push refreshed topology (IP, name, grouping) into live renderers."""
        if self._topology is None:
            return
        for uid, renderer in list(self._by_zone.items()):
            zone = self._topology.zone(uid)
            if zone is None:
                continue
            renamed = zone.name != renderer.zone.name
            renderer.update_zone(zone)
            if renamed and self._ssdp:
                LOGGER.info("Room renamed to %s; re-announcing", zone.name)
                self._ssdp.announce_alive(renderer)

    # ------------------------------------------------------------------
    async def _discovery_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.discovery_interval)
            with contextlib.suppress(Exception):
                await self._topology.discover()

    async def _topology_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.topology_interval)
            try:
                await self._topology.refresh()
                self._sync_zone_data()
            except Exception:
                LOGGER.exception("Topology refresh failed")

    async def _subscription_loop(self) -> None:
        while True:
            await asyncio.sleep(RENEW_CHECK_INTERVAL)
            for renderer in list(self.renderers.values()):
                if renderer.subscriptions:
                    await renderer.subscriptions.drop_expired()
                link = self._links.get(renderer.uuid)
                if link is None:
                    continue
                try:
                    await link.ensure()
                except Exception:
                    LOGGER.debug("Subscription upkeep failed for %s", renderer.zone.name)

    async def _poll_loop(self) -> None:
        """Safety net: reconcile state for rooms a control point is watching."""
        while True:
            await asyncio.sleep(self.config.poll_interval)
            for renderer in list(self.renderers.values()):
                if renderer.subscriptions and renderer.subscriptions.has_subscribers():
                    with contextlib.suppress(Exception):
                        await renderer.refresh()
