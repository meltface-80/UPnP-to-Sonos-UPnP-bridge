"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field

# Stable namespace so a given Sonos player always maps to the same virtual
# renderer UUID, even across bridge restarts / reinstalls.  Control points cache
# device UUIDs, so this needs to be deterministic.
UUID_NAMESPACE = uuid.UUID("6f9d1b3e-3a1d-5d5a-9f4b-2b4a6c8d0e11")

BRIDGE_NAME = "Sonos UPnP Bridge"
BRIDGE_VERSION = "1.0.2"

#: UPnP's "the description document has changed, read it again" signal, sent in
#: SSDP as CONFIGID.UPNP.ORG and carried on <root>.  Deriving it from the
#: version means every release invalidates whatever a control point cached; it
#: has to fit in 31 bits.
CONFIG_ID = (
    int(hashlib.blake2s(BRIDGE_VERSION.encode("utf-8"), digest_size=4).hexdigest(), 16)
    & 0x7FFFFFFF
)


def _str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None else value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    raw = _str(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Config:
    """All tunables.  Every field maps to an environment variable."""

    # Networking -----------------------------------------------------------
    bridge_ip: str = ""
    http_port: int = 1500
    ssdp_port: int = 1900
    multicast_ttl: int = 4

    # Naming ---------------------------------------------------------------
    name_suffix: str = " (Sonos)"

    # Behaviour ------------------------------------------------------------
    mode: str = "queue"  # "queue" (gapless, via the Sonos queue) or "direct"
    ungroup_on_play: bool = False
    include_zones: list[str] = field(default_factory=list)
    exclude_zones: list[str] = field(default_factory=list)
    static_hosts: list[str] = field(default_factory=list)

    # Timing ---------------------------------------------------------------
    discovery_interval: float = 60.0
    discovery_mx: int = 2
    discovery_attempts: int = 3
    topology_interval: float = 30.0
    ssdp_alive_interval: float = 300.0
    ssdp_max_age: int = 1800
    poll_interval: float = 10.0
    sonos_sub_timeout: int = 600
    event_timeout: int = 1800
    http_timeout: float = 10.0

    # Misc -----------------------------------------------------------------
    log_level: str = "INFO"
    extra_protocol_info: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> Config:
        mode = _str("BRIDGE_MODE", "queue").strip().lower()
        if mode not in ("queue", "direct"):
            mode = "queue"
        return cls(
            bridge_ip=_str("BRIDGE_IP", "").strip(),
            http_port=_int("HTTP_PORT", 1500),
            ssdp_port=_int("SSDP_PORT", 1900),
            multicast_ttl=_int("MULTICAST_TTL", 4),
            name_suffix=_str("NAME_SUFFIX", " (Sonos)"),
            mode=mode,
            ungroup_on_play=_bool("UNGROUP_ON_PLAY", False),
            include_zones=_list("INCLUDE_ZONES"),
            exclude_zones=_list("EXCLUDE_ZONES"),
            static_hosts=_list("SONOS_HOSTS"),
            discovery_interval=_float("DISCOVERY_INTERVAL", 60.0),
            discovery_mx=_int("DISCOVERY_MX", 2),
            discovery_attempts=_int("DISCOVERY_ATTEMPTS", 3),
            topology_interval=_float("TOPOLOGY_INTERVAL", 30.0),
            ssdp_alive_interval=_float("SSDP_ALIVE_INTERVAL", 300.0),
            ssdp_max_age=_int("SSDP_MAX_AGE", 1800),
            poll_interval=_float("POLL_INTERVAL", 10.0),
            sonos_sub_timeout=_int("SONOS_SUB_TIMEOUT", 600),
            event_timeout=_int("EVENT_TIMEOUT", 1800),
            http_timeout=_float("HTTP_TIMEOUT", 10.0),
            log_level=_str("LOG_LEVEL", "INFO").strip().upper(),
            extra_protocol_info=_list("EXTRA_PROTOCOL_INFO"),
        )

    def zone_allowed(self, zone_name: str) -> bool:
        """Apply the INCLUDE_ZONES / EXCLUDE_ZONES filters (case-insensitive)."""
        name = zone_name.casefold()
        if self.include_zones and not any(
            name == zone.casefold() for zone in self.include_zones
        ):
            return False
        return not any(name == zone.casefold() for zone in self.exclude_zones)

    def udn_for(self, sonos_uid: str) -> str:
        """Deterministic virtual-renderer UDN for a Sonos player UID."""
        return "uuid:" + str(uuid.uuid5(UUID_NAMESPACE, f"sonos-bridge:{sonos_uid}"))
