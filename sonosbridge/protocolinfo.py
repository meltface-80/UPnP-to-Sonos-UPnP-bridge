"""The Sink protocolInfo the bridge advertises to Audirvana.

Audirvana consults ConnectionManager::GetProtocolInfo to decide which formats it
may stream to a device and which it must convert first.  A Sonos player's own
protocolInfo is a sprawling list full of Sonos-private schemes
(``x-file-cifs``, ``x-rincon-mp3radio``, ``x-sonos-spotify`` ...) which are
meaningless to a control point, so the list is filtered down to plain HTTP audio
and topped up with the formats Sonos is known to accept over HTTP.
"""

from __future__ import annotations

# Formats Sonos players accept from a third-party HTTP server.  Sample-rate and
# bit-depth limits are a per-model property that protocolInfo cannot express -
# see the README for how to cap those in Audirvana itself.
BASE_SINK = (
    "http-get:*:audio/mpeg:*",
    "http-get:*:audio/mp3:*",
    "http-get:*:audio/mp4:*",
    "http-get:*:audio/x-m4a:*",
    "http-get:*:audio/aac:*",
    "http-get:*:audio/x-aac:*",
    "http-get:*:audio/flac:*",
    "http-get:*:audio/x-flac:*",
    "http-get:*:audio/wav:*",
    "http-get:*:audio/x-wav:*",
    "http-get:*:audio/wave:*",
    "http-get:*:audio/aiff:*",
    "http-get:*:audio/x-aiff:*",
    "http-get:*:audio/ogg:*",
    "http-get:*:application/ogg:*",
    "http-get:*:audio/x-ms-wma:*",
    "http-get:*:audio/L16;rate=44100;channels=2:*",
    "http-get:*:audio/L16;rate=48000;channels=2:*",
)


def _is_http_audio(entry: str) -> bool:
    parts = entry.split(":")
    if len(parts) < 3 or parts[0].strip().lower() != "http-get":
        return False
    mime = parts[2].strip().lower()
    return mime.startswith("audio/") or mime == "application/ogg"


def build_sink(sonos_sink: str = "", extra: tuple[str, ...] | list[str] = ()) -> str:
    """Merge the player's own HTTP entries with the known-good base list."""
    seen: dict[str, None] = {}
    for entry in BASE_SINK:
        seen[entry] = None
    for entry in (sonos_sink or "").split(","):
        entry = entry.strip()
        if entry and _is_http_audio(entry):
            seen.setdefault(entry, None)
    for entry in extra:
        entry = entry.strip()
        if entry:
            seen.setdefault(entry, None)
    return ",".join(seen)
