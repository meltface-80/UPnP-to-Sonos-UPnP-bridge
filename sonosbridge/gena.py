"""GENA eventing - both directions.

* :class:`SubscriptionManager` serves control points (Audirvana) that subscribe
  to a virtual renderer's services.
* :class:`SonosSubscription` keeps a subscription open on a real Sonos player so
  state changes reach the bridge without polling.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

import aiohttp

from .lastchange import propertyset

LOGGER = logging.getLogger(__name__)

MAX_SEQ = 0xFFFFFFFF
MAX_NOTIFY_FAILURES = 3


def parse_timeout(header: str, default: int) -> int:
    """Parse a GENA ``TIMEOUT: Second-1800`` header."""
    value = (header or "").strip()
    if not value or value.lower() == "infinite":
        return default
    if value.lower().startswith("second-"):
        tail = value.split("-", 1)[1].strip()
        if tail.lower() == "infinite":
            return default
        try:
            return max(60, int(tail))
        except ValueError:
            return default
    return default


def parse_callbacks(header: str) -> list[str]:
    """Parse a ``CALLBACK: <url1><url2>`` header into a list of URLs."""
    urls: list[str] = []
    for chunk in (header or "").split("<"):
        url, sep, _ = chunk.partition(">")
        if sep and url.strip().lower().startswith("http"):
            urls.append(url.strip())
    return urls


@dataclass
class Subscription:
    sid: str
    callbacks: list[str]
    service_id: str
    timeout: int
    expires_at: float
    seq: int = 0
    failures: int = 0

    def renew(self, timeout: int) -> None:
        self.timeout = timeout
        self.expires_at = time.monotonic() + timeout

    @property
    def expired(self) -> bool:
        return time.monotonic() > self.expires_at

    def next_seq(self) -> int:
        seq = self.seq
        # SEQ 0 is reserved for the initial event; after wrapping, restart at 1.
        self.seq = 1 if self.seq >= MAX_SEQ else self.seq + 1
        return seq


class SubscriptionManager:
    """Tracks control-point subscriptions and pushes NOTIFY messages to them."""

    def __init__(self, session: aiohttp.ClientSession, default_timeout: int = 1800) -> None:
        self._session = session
        self._default_timeout = default_timeout
        self._subscriptions: dict[str, Subscription] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    @property
    def default_timeout(self) -> int:
        return self._default_timeout

    def __len__(self) -> int:
        return len(self._subscriptions)

    def count_for(self, service_id: str) -> int:
        return sum(1 for s in self._subscriptions.values() if s.service_id == service_id)

    def has_subscribers(self, service_id: str | None = None) -> bool:
        if service_id is None:
            return bool(self._subscriptions)
        return self.count_for(service_id) > 0

    def get(self, sid: str) -> Subscription | None:
        return self._subscriptions.get(sid)

    async def subscribe(
        self, service_id: str, callbacks: list[str], requested_timeout: int
    ) -> Subscription:
        sid = "uuid:" + str(uuid.uuid4())
        timeout = max(60, min(requested_timeout or self._default_timeout, 86400))
        subscription = Subscription(
            sid=sid,
            callbacks=callbacks,
            service_id=service_id,
            timeout=timeout,
            expires_at=time.monotonic() + timeout,
        )
        async with self._lock:
            self._subscriptions[sid] = subscription
        LOGGER.debug("New subscription %s for %s -> %s", sid, service_id, callbacks)
        return subscription

    async def renew(self, sid: str, requested_timeout: int) -> Subscription | None:
        async with self._lock:
            subscription = self._subscriptions.get(sid)
            if subscription is None:
                return None
            subscription.renew(max(60, min(requested_timeout or self._default_timeout, 86400)))
            return subscription

    async def unsubscribe(self, sid: str) -> bool:
        async with self._lock:
            return self._subscriptions.pop(sid, None) is not None

    async def drop_expired(self) -> None:
        async with self._lock:
            stale = [sid for sid, sub in self._subscriptions.items() if sub.expired]
            for sid in stale:
                self._subscriptions.pop(sid, None)
        for sid in stale:
            LOGGER.debug("Subscription %s expired", sid)

    async def notify(self, service_id: str, properties: Mapping[str, str]) -> None:
        """Send a propchange NOTIFY to every subscriber of *service_id*."""
        targets = [s for s in self._subscriptions.values() if s.service_id == service_id]
        if not targets:
            return
        body = propertyset(properties).encode("utf-8")
        await asyncio.gather(
            *(self._notify_one(sub, body) for sub in targets), return_exceptions=True
        )

    async def notify_initial(self, subscription: Subscription, properties: Mapping[str, str]) -> None:
        """Send the mandatory initial event right after a successful SUBSCRIBE."""
        await self._notify_one(subscription, propertyset(properties).encode("utf-8"))

    def send_initial(self, subscription: Subscription, properties: Mapping[str, str]) -> None:
        """Queue the initial event so it is sent after the SUBSCRIBE response."""
        task = asyncio.create_task(
            self.notify_initial(subscription, properties),
            name=f"initial-event-{subscription.sid}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _notify_one(self, subscription: Subscription, body: bytes) -> None:
        seq = subscription.next_seq()
        headers = {
            "CONTENT-TYPE": 'text/xml; charset="utf-8"',
            "NT": "upnp:event",
            "NTS": "upnp:propchange",
            "SID": subscription.sid,
            "SEQ": str(seq),
            "Connection": "close",
        }
        for url in subscription.callbacks:
            try:
                async with self._session.request(
                    "NOTIFY",
                    url,
                    data=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    await response.release()
                    if response.status < 400:
                        subscription.failures = 0
                        return
                    LOGGER.debug("NOTIFY %s -> HTTP %s", url, response.status)
            except (TimeoutError, aiohttp.ClientError, OSError) as exc:
                LOGGER.debug("NOTIFY %s failed: %s", url, exc)

        subscription.failures += 1
        if subscription.failures >= MAX_NOTIFY_FAILURES:
            LOGGER.info(
                "Dropping subscription %s after %d failed notifications",
                subscription.sid,
                subscription.failures,
            )
            await self.unsubscribe(subscription.sid)


class SonosSubscription:
    """One GENA subscription held against a real Sonos service."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        event_url: str,
        callback_url: str,
        timeout: int = 600,
    ) -> None:
        self._session = session
        self.event_url = event_url
        self.callback_url = callback_url
        self.requested_timeout = timeout
        self.sid: str = ""
        self.expires_at: float = 0.0

    @property
    def active(self) -> bool:
        return bool(self.sid) and time.monotonic() < self.expires_at

    async def subscribe(self) -> bool:
        headers = {
            "CALLBACK": f"<{self.callback_url}>",
            "NT": "upnp:event",
            "TIMEOUT": f"Second-{self.requested_timeout}",
        }
        try:
            async with self._session.request(
                "SUBSCRIBE",
                self.event_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                await response.release()
                if response.status != 200:
                    LOGGER.debug("SUBSCRIBE %s -> HTTP %s", self.event_url, response.status)
                    return False
                self.sid = response.headers.get("SID", "")
                granted = parse_timeout(
                    response.headers.get("TIMEOUT", ""), self.requested_timeout
                )
                self.expires_at = time.monotonic() + granted
                return bool(self.sid)
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            LOGGER.debug("SUBSCRIBE %s failed: %s", self.event_url, exc)
            return False

    async def renew(self) -> bool:
        if not self.sid:
            return await self.subscribe()
        headers = {"SID": self.sid, "TIMEOUT": f"Second-{self.requested_timeout}"}
        try:
            async with self._session.request(
                "SUBSCRIBE",
                self.event_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                await response.release()
                if response.status != 200:
                    # The player forgot us (reboot, firmware update): start over.
                    self.sid = ""
                    return await self.subscribe()
                granted = parse_timeout(
                    response.headers.get("TIMEOUT", ""), self.requested_timeout
                )
                self.expires_at = time.monotonic() + granted
                return True
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            LOGGER.debug("Renew %s failed: %s", self.event_url, exc)
            self.sid = ""
            return False

    async def unsubscribe(self) -> None:
        if not self.sid:
            return
        try:
            async with self._session.request(
                "UNSUBSCRIBE",
                self.event_url,
                headers={"SID": self.sid},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                await response.release()
        except (TimeoutError, aiohttp.ClientError, OSError):
            pass
        finally:
            self.sid = ""
            self.expires_at = 0.0
