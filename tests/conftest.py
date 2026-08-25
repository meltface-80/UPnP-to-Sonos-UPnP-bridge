"""Shared test doubles: a simulated Sonos player and a stub topology."""

from __future__ import annotations

import pytest

from sonosbridge import didl
from sonosbridge.config import Config
from sonosbridge.renderer import VirtualRenderer
from sonosbridge.soap import UPnPError
from sonosbridge.sonos import ZoneInfo


class FakeSonos:
    """A stand-in for a real player that behaves like the Sonos queue model."""

    def __init__(self, uid: str = "RINCON_AAA01400", name: str = "Kitchen") -> None:
        self.uid = uid
        self.name = name
        self.calls: list[tuple[str, dict]] = []
        self.call_urls: list[str] = []
        self.queue: list[tuple[str, str]] = []  # (uri, metadata)
        self.track_index = 1
        self.transport_state = "STOPPED"
        self.av_transport_uri = ""
        self.play_mode = "NORMAL"
        self.volume = 20
        self.mute = False
        self.position = "0:00:30"
        self.duration = "0:04:00"
        self.errors: dict[str, UPnPError] = {}
        self.protocol_info_sink = (
            "http-get:*:audio/mpeg:*,x-file-cifs:*:audio/flac:*,"
            "x-rincon-mp3radio:*:*:*,http-get:*:audio/x-sonos-oggvorbis:*"
        )

    # -- helpers used by tests -------------------------------------------
    def actions(self) -> list[str]:
        return [action for action, _ in self.calls]

    def args_for(self, action: str) -> dict:
        for name, args in self.calls:
            if name == action:
                return args
        return {}

    def urls_for(self, action: str) -> list[str]:
        return [u for (a, _), u in zip(self.calls, self.call_urls, strict=True) if a == action]

    def current_uri(self) -> str:
        if not self.queue:
            return ""
        index = min(max(self.track_index, 1), len(self.queue))
        return self.queue[index - 1][0]

    def advance(self) -> None:
        """Simulate Sonos moving on to the next queued track."""
        if self.track_index < len(self.queue):
            self.track_index += 1

    # -- the SoapClient interface ----------------------------------------
    async def call(self, url: str, service_type: str, action: str, args) -> dict:
        args = dict(args or {})
        self.calls.append((action, args))
        self.call_urls.append(url)
        if action in self.errors:
            raise self.errors[action]
        handler = getattr(self, f"_{action}", None)
        if handler is None:
            raise UPnPError(401, f"FakeSonos does not implement {action}")
        return handler(args) or {}

    # -- transport --------------------------------------------------------
    def _Play(self, args):
        self.transport_state = "PLAYING"

    def _Pause(self, args):
        self.transport_state = "PAUSED_PLAYBACK"

    def _Stop(self, args):
        self.transport_state = "STOPPED"

    def _Next(self, args):
        self.advance()

    def _Previous(self, args):
        self.track_index = max(1, self.track_index - 1)

    def _Seek(self, args):
        if args.get("Unit") == "TRACK_NR":
            self.track_index = int(args.get("Target", 1))
        else:
            self.position = args.get("Target", "0:00:00")

    def _SetPlayMode(self, args):
        self.play_mode = args.get("NewPlayMode", "NORMAL")

    def _SetAVTransportURI(self, args):
        self.av_transport_uri = args.get("CurrentURI", "")
        if not self.av_transport_uri.startswith("x-rincon-queue:"):
            self.queue = [(self.av_transport_uri, args.get("CurrentURIMetaData", ""))]
            self.track_index = 1

    def _SetNextAVTransportURI(self, args):
        raise UPnPError(402, "Invalid Args")

    def _GetTransportInfo(self, args):
        return {
            "CurrentTransportState": self.transport_state,
            "CurrentTransportStatus": "OK",
            "CurrentSpeed": "1",
        }

    def _GetPositionInfo(self, args):
        return {
            "Track": str(self.track_index),
            "TrackDuration": self.duration,
            "TrackMetaData": self.queue[self.track_index - 1][1] if self.queue else "",
            "TrackURI": self.current_uri(),
            "RelTime": self.position,
            "AbsTime": "NOT_IMPLEMENTED",
            "RelCount": "2147483647",
            "AbsCount": "2147483647",
        }

    def _GetTransportSettings(self, args):
        return {"PlayMode": self.play_mode, "RecQualityMode": "NOT_IMPLEMENTED"}

    # -- queue ------------------------------------------------------------
    def _RemoveAllTracksFromQueue(self, args):
        self.queue = []
        self.track_index = 1

    def _AddURIToQueue(self, args):
        entry = (args.get("EnqueuedURI", ""), args.get("EnqueuedURIMetaData", ""))
        desired = int(args.get("DesiredFirstTrackNumberEnqueued", 0) or 0)
        if desired and desired <= len(self.queue):
            self.queue.insert(desired - 1, entry)
            first = desired
        else:
            self.queue.append(entry)
            first = len(self.queue)
        return {
            "FirstTrackNumberEnqueued": str(first),
            "NumTracksAdded": "1",
            "NewQueueLength": str(len(self.queue)),
        }

    def _RemoveTrackRangeFromQueue(self, args):
        start = int(args.get("StartingIndex", 1))
        count = int(args.get("NumberOfTracks", 0))
        del self.queue[start - 1 : start - 1 + count]
        return {"NewUpdateID": "1"}

    def _BecomeCoordinatorOfStandaloneGroup(self, args):
        return {}

    # -- rendering --------------------------------------------------------
    def _GetVolume(self, args):
        return {"CurrentVolume": str(self.volume)}

    def _SetVolume(self, args):
        self.volume = int(args.get("DesiredVolume", 0))

    def _GetMute(self, args):
        return {"CurrentMute": "1" if self.mute else "0"}

    def _SetMute(self, args):
        self.mute = args.get("DesiredMute") in (1, "1", True)

    def _GetProtocolInfo(self, args):
        return {"Source": "", "Sink": self.protocol_info_sink}


class StubTopology:
    """Minimal topology: every zone coordinates itself unless told otherwise."""

    def __init__(self, zones: dict[str, ZoneInfo] | None = None) -> None:
        self.zones = zones or {}

    def zone(self, uid: str):
        return self.zones.get(uid)

    def coordinator_for(self, uid: str):
        zone = self.zones.get(uid)
        if zone is None:
            return None
        if zone.is_coordinator:
            return zone
        return self.zones.get(zone.coordinator_uid) or zone

    async def refresh(self):
        return False


def make_didl(uri: str, title: str = "Track", duration: str = "0:04:00") -> str:
    meta = didl.TrackMetadata(title=title, creator="Artist", album="Album", duration=duration)
    return didl.build(uri, meta)


@pytest.fixture
def zone() -> ZoneInfo:
    return ZoneInfo(
        uid="RINCON_AAA01400",
        name="Kitchen",
        ip="192.168.1.10",
        coordinator_uid="RINCON_AAA01400",
        model="Sonos One",
    )


@pytest.fixture
def config() -> Config:
    return Config(name_suffix=" (Sonos)", mode="queue")


@pytest.fixture
def fake_sonos() -> FakeSonos:
    return FakeSonos()


@pytest.fixture
def renderer(config, zone, fake_sonos) -> VirtualRenderer:
    topology = StubTopology({zone.uid: zone})
    return VirtualRenderer(config, zone, topology, fake_sonos, "http://192.168.1.2:1500")
