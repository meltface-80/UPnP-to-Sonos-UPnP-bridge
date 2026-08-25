"""SSDP for the virtual renderers: announce them, and answer searches.

Audirvana finds renderers exactly the way any UPnP control point does - it
multicasts an M-SEARCH and listens for NOTIFY announcements.  This module makes
the bridge's virtual devices visible to it, and doubles as a passive listener
for real Sonos announcements so newly powered-on players are picked up quickly.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
from collections.abc import Callable, Iterable

from .net import SSDP_ADDR, SSDP_PORT, host_from_url, make_ssdp_socket, parse_headers
from .renderer import MEDIA_RENDERER, SERVICE_IDS, VirtualRenderer
from .sonos import ZONE_PLAYER_ST

LOGGER = logging.getLogger(__name__)

ROOT_DEVICE = "upnp:rootdevice"
SSDP_ALL = "ssdp:all"
MAX_RESPONSE_DELAY = 3.0


def server_header() -> str:
    from .config import BRIDGE_NAME, BRIDGE_VERSION

    slug = BRIDGE_NAME.replace(" ", "")
    return f"Linux/5.x UPnP/1.0 {slug}/{BRIDGE_VERSION}"


def notification_targets(renderer: VirtualRenderer) -> list[tuple[str, str]]:
    """The ``(NT, USN)`` pairs a MediaRenderer must announce."""
    udn = renderer.udn
    targets = [
        (ROOT_DEVICE, f"{udn}::{ROOT_DEVICE}"),
        (udn, udn),
        (MEDIA_RENDERER, f"{udn}::{MEDIA_RENDERER}"),
    ]
    targets.extend((service, f"{udn}::{service}") for service in SERVICE_IDS)
    return targets


def matches(search_target: str, notification_type: str) -> bool:
    search_target = (search_target or "").strip()
    if search_target in (SSDP_ALL, ""):
        return True
    return search_target == notification_type


class _SsdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: SsdpServer) -> None:
        self._server = server

    def connection_made(self, transport) -> None:
        self._server.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self._server.handle_datagram(data, addr)

    def error_received(self, exc) -> None:  # pragma: no cover - transient ICMP
        LOGGER.debug("SSDP socket error: %s", exc)


class SsdpServer:
    """Announces the virtual renderers and replies to M-SEARCH."""

    def __init__(
        self,
        bind_ip: str,
        renderers: Callable[[], Iterable[VirtualRenderer]],
        max_age: int = 1800,
        alive_interval: float = 300.0,
        ttl: int = 4,
        port: int = SSDP_PORT,
        boot_id: int = 1,
        on_sonos_seen: Callable[[str], None] | None = None,
    ) -> None:
        self.bind_ip = bind_ip
        self.max_age = max_age
        self.alive_interval = alive_interval
        self.ttl = ttl
        self.port = port
        self.boot_id = boot_id
        self.transport: asyncio.DatagramTransport | None = None
        self._renderers = renderers
        self._on_sonos_seen = on_sonos_seen
        self._task: asyncio.Task | None = None
        self._server = server_header()

    # ------------------------------------------------------------------
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = make_ssdp_socket(self.bind_ip, self.port, self.ttl)
        await loop.create_datagram_endpoint(lambda: _SsdpProtocol(self), sock=sock)
        self._task = asyncio.create_task(self._alive_loop(), name="ssdp-alive")
        LOGGER.info("SSDP listening on %s:%d", self.bind_ip, self.port)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.announce_byebye_all()
        if self.transport:
            self.transport.close()
            self.transport = None

    # ------------------------------------------------------------------
    def handle_datagram(self, data: bytes, addr) -> None:
        headers = parse_headers(data)
        start_line = headers.get("", "").upper()

        if start_line.startswith("M-SEARCH"):
            self._handle_search(headers, addr)
            return

        if start_line.startswith("NOTIFY") and self._on_sonos_seen:
            # A real Sonos announcing itself is a free discovery hint.
            notification_type = headers.get("nt", "")
            usn = headers.get("usn", "")
            if ZONE_PLAYER_ST in notification_type or "RINCON_" in usn:
                if headers.get("nts", "") == "ssdp:byebye":
                    return
                host = host_from_url(headers.get("location", "")) or addr[0]
                if host:
                    self._on_sonos_seen(host)

    def _handle_search(self, headers: dict[str, str], addr) -> None:
        if headers.get("man", "").strip('"') != "ssdp:discover":
            return
        search_target = headers.get("st", "")
        try:
            mx = max(0.0, min(float(headers.get("mx", "1") or 1), MAX_RESPONSE_DELAY))
        except ValueError:
            mx = 1.0

        replies: list[tuple[str, str, str]] = []
        for renderer in self._renderers():
            for notification_type, usn in notification_targets(renderer):
                if matches(search_target, notification_type):
                    replies.append((notification_type, usn, renderer.description_url))
        if not replies:
            return

        # The spec asks responders to spread replies over the MX window so a
        # control point is not flooded by every device at once.
        delay = random.uniform(0, mx) if mx else 0.0
        asyncio.get_running_loop().call_later(delay, self._send_replies, replies, addr)

    def _send_replies(self, replies, addr) -> None:
        if self.transport is None:
            return
        for notification_type, usn, location in replies:
            message = (
                "HTTP/1.1 200 OK\r\n"
                f"CACHE-CONTROL: max-age={self.max_age}\r\n"
                f"DATE: {email.utils.formatdate(usegmt=True)}\r\n"
                "EXT:\r\n"
                f"LOCATION: {location}\r\n"
                f"SERVER: {self._server}\r\n"
                f"ST: {notification_type}\r\n"
                f"USN: {usn}\r\n"
                f"BOOTID.UPNP.ORG: {self.boot_id}\r\n"
                "CONFIGID.UPNP.ORG: 1\r\n"
                "\r\n"
            ).encode()
            try:
                self.transport.sendto(message, addr)
            except OSError as exc:  # pragma: no cover - transient
                LOGGER.debug("SSDP reply to %s failed: %s", addr, exc)

    # ------------------------------------------------------------------
    def announce_alive(self, renderer: VirtualRenderer) -> None:
        self._notify(renderer, "ssdp:alive")

    def announce_byebye(self, renderer: VirtualRenderer) -> None:
        self._notify(renderer, "ssdp:byebye")

    def announce_byebye_all(self) -> None:
        for renderer in self._renderers():
            self.announce_byebye(renderer)

    def _notify(self, renderer: VirtualRenderer, nts: str) -> None:
        if self.transport is None:
            return
        for notification_type, usn in notification_targets(renderer):
            lines = [
                "NOTIFY * HTTP/1.1",
                f"HOST: {SSDP_ADDR}:{SSDP_PORT}",
                f"NT: {notification_type}",
                f"NTS: {nts}",
                f"USN: {usn}",
                f"BOOTID.UPNP.ORG: {self.boot_id}",
                "CONFIGID.UPNP.ORG: 1",
            ]
            if nts == "ssdp:alive":
                lines[2:2] = [
                    f"CACHE-CONTROL: max-age={self.max_age}",
                    f"LOCATION: {renderer.description_url}",
                    f"SERVER: {self._server}",
                ]
            message = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
            try:
                self.transport.sendto(message, (SSDP_ADDR, SSDP_PORT))
            except OSError as exc:  # pragma: no cover - transient
                LOGGER.debug("SSDP notify failed: %s", exc)

    async def _alive_loop(self) -> None:
        # UPnP requires the initial alive burst to be sent more than once,
        # because SSDP is unreliable by design.
        while True:
            for renderer in self._renderers():
                self.announce_alive(renderer)
            await asyncio.sleep(self.alive_interval)
