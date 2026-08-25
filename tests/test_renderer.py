"""The playback flows Audirvana actually drives, against a simulated player."""

from __future__ import annotations

import pytest

from sonosbridge.config import Config
from sonosbridge.renderer import VirtualRenderer
from sonosbridge.soap import UPnPError
from sonosbridge.sonos import ZoneInfo

from .conftest import StubTopology, make_didl

TRACK1 = "http://192.168.1.5:52341/1.flac"
TRACK2 = "http://192.168.1.5:52341/2.flac"
TRACK3 = "http://192.168.1.5:52341/3.flac"


async def set_uri(renderer, uri, meta=None):
    return await renderer.handle_action(
        "AVTransport",
        "SetAVTransportURI",
        {"InstanceID": "0", "CurrentURI": uri, "CurrentURIMetaData": meta or make_didl(uri)},
    )


async def set_next(renderer, uri, meta=None):
    return await renderer.handle_action(
        "AVTransport",
        "SetNextAVTransportURI",
        {"InstanceID": "0", "NextURI": uri, "NextURIMetaData": meta or make_didl(uri)},
    )


# ----------------------------------------------------------------------
# Loading a track
# ----------------------------------------------------------------------
async def test_loading_a_track_uses_the_sonos_queue(renderer, fake_sonos):
    await set_uri(renderer, TRACK1)

    assert fake_sonos.actions()[:4] == [
        "RemoveAllTracksFromQueue",
        "AddURIToQueue",
        "SetAVTransportURI",
        "Seek",
    ]
    assert fake_sonos.queue[0][0] == TRACK1
    assert fake_sonos.av_transport_uri == "x-rincon-queue:RINCON_AAA01400#0"
    assert fake_sonos.args_for("Seek") == {
        "InstanceID": 0,
        "Unit": "TRACK_NR",
        "Target": "1",
    }


async def test_metadata_is_rewritten_for_sonos(renderer, fake_sonos):
    await set_uri(renderer, TRACK1)
    enqueued = fake_sonos.args_for("AddURIToQueue")["EnqueuedURIMetaData"]
    assert "RINCON_AssociatedZPUDN" in enqueued
    assert 'protocolInfo="http-get:*:audio/flac:*"' in enqueued


async def test_shuffle_and_repeat_are_turned_off_on_load(renderer, fake_sonos):
    fake_sonos.play_mode = "SHUFFLE"
    await set_uri(renderer, TRACK1)
    assert fake_sonos.play_mode == "NORMAL"


async def test_play_pause_stop(renderer, fake_sonos):
    await set_uri(renderer, TRACK1)
    await renderer.handle_action("AVTransport", "Play", {"InstanceID": "0", "Speed": "1"})
    assert fake_sonos.transport_state == "PLAYING"
    assert fake_sonos.args_for("Play")["Speed"] == "1"

    await renderer.handle_action("AVTransport", "Pause", {"InstanceID": "0"})
    assert fake_sonos.transport_state == "PAUSED_PLAYBACK"

    await renderer.handle_action("AVTransport", "Stop", {"InstanceID": "0"})
    assert fake_sonos.transport_state == "STOPPED"


async def test_empty_uri_stops_and_clears(renderer, fake_sonos):
    await set_uri(renderer, TRACK1)
    await set_uri(renderer, "", "")
    assert fake_sonos.queue == []
    assert renderer.state.current_uri == ""


# ----------------------------------------------------------------------
# Gapless: the whole track-change handshake
# ----------------------------------------------------------------------
async def test_next_track_is_appended_to_the_queue(renderer, fake_sonos):
    await set_uri(renderer, TRACK1)
    await set_next(renderer, TRACK2)
    assert [uri for uri, _ in fake_sonos.queue] == [TRACK1, TRACK2]
    assert renderer.state.next_uri == TRACK2


async def test_replacing_the_next_track_does_not_grow_the_queue(renderer, fake_sonos):
    await set_uri(renderer, TRACK1)
    await set_next(renderer, TRACK2)
    await set_next(renderer, TRACK3)
    assert [uri for uri, _ in fake_sonos.queue] == [TRACK1, TRACK3]


async def test_clearing_the_next_track_trims_the_queue(renderer, fake_sonos):
    await set_uri(renderer, TRACK1)
    await set_next(renderer, TRACK2)
    await set_next(renderer, "", "")
    assert [uri for uri, _ in fake_sonos.queue] == [TRACK1]
    assert renderer.state.next_uri == ""


async def test_resending_the_current_track_does_not_restart_playback(renderer, fake_sonos):
    await set_uri(renderer, TRACK1)
    await renderer.handle_action("AVTransport", "Play", {"InstanceID": "0", "Speed": "1"})
    before = fake_sonos.actions().count("RemoveAllTracksFromQueue")

    await set_uri(renderer, TRACK1)

    assert fake_sonos.actions().count("RemoveAllTracksFromQueue") == before


async def test_full_gapless_handshake(renderer, fake_sonos):
    """Load, queue the next track, let Sonos advance, then replay Audirvana's
    follow-up calls - none of which may disturb playback."""
    await set_uri(renderer, TRACK1)
    await renderer.handle_action("AVTransport", "Play", {"InstanceID": "0", "Speed": "1"})
    await set_next(renderer, TRACK2)

    fake_sonos.advance()  # Sonos rolls into track 2 on its own
    resets_before = fake_sonos.actions().count("RemoveAllTracksFromQueue")

    position = await renderer.handle_action("AVTransport", "GetPositionInfo", {"InstanceID": "0"})
    assert position["TrackURI"] == TRACK2
    assert position["Track"] == "1"  # the bridge always presents a single track
    assert renderer.state.next_uri == ""

    # Audirvana now confirms the new current track and queues the one after it.
    await set_uri(renderer, TRACK2)
    await set_next(renderer, TRACK3)

    assert fake_sonos.actions().count("RemoveAllTracksFromQueue") == resets_before
    assert fake_sonos.transport_state == "PLAYING"
    assert [uri for uri, _ in fake_sonos.queue][-1] == TRACK3


async def test_next_track_sent_before_sonos_advances_is_not_reloaded(renderer, fake_sonos):
    await set_uri(renderer, TRACK1)
    await renderer.handle_action("AVTransport", "Play", {"InstanceID": "0", "Speed": "1"})
    await set_next(renderer, TRACK2)
    resets_before = fake_sonos.actions().count("RemoveAllTracksFromQueue")

    await set_uri(renderer, TRACK2)  # arrives early

    assert fake_sonos.actions().count("RemoveAllTracksFromQueue") == resets_before
    assert renderer.state.current_uri == TRACK2


async def test_a_new_track_while_stopped_loads_normally(renderer, fake_sonos):
    await set_uri(renderer, TRACK1)
    await set_next(renderer, TRACK2)
    renderer.state.transport_state = "STOPPED"

    await set_uri(renderer, TRACK3)

    assert [uri for uri, _ in fake_sonos.queue] == [TRACK3]


# ----------------------------------------------------------------------
# Reporting back to the control point
# ----------------------------------------------------------------------
async def test_position_info_reports_the_uri_audirvana_gave_us(renderer, fake_sonos):
    await set_uri(renderer, TRACK1)
    info = await renderer.handle_action("AVTransport", "GetPositionInfo", {"InstanceID": "0"})
    assert info["TrackURI"] == TRACK1
    assert info["TrackDuration"] == "0:04:00"
    assert info["RelTime"] == "0:00:30"
    assert "<dc:title>" in info["TrackMetaData"]


async def test_media_info_presents_a_single_track_transport(renderer):
    await set_uri(renderer, TRACK1)
    await set_next(renderer, TRACK2)
    info = await renderer.handle_action("AVTransport", "GetMediaInfo", {"InstanceID": "0"})
    assert info["NrTracks"] == "1"
    assert info["CurrentURI"] == TRACK1
    assert info["NextURI"] == TRACK2
    assert info["PlayMedium"] == "NETWORK"


async def test_transport_info_passes_the_player_state_through(renderer, fake_sonos):
    fake_sonos.transport_state = "TRANSITIONING"
    info = await renderer.handle_action("AVTransport", "GetTransportInfo", {"InstanceID": "0"})
    assert info["CurrentTransportState"] == "TRANSITIONING"


# ----------------------------------------------------------------------
# Volume
# ----------------------------------------------------------------------
async def test_volume_and_mute_round_trip(renderer, fake_sonos):
    await renderer.handle_action(
        "RenderingControl",
        "SetVolume",
        {"InstanceID": "0", "Channel": "Master", "DesiredVolume": "42"},
    )
    assert fake_sonos.volume == 42
    result = await renderer.handle_action(
        "RenderingControl", "GetVolume", {"InstanceID": "0", "Channel": "Master"}
    )
    assert result["CurrentVolume"] == "42"

    await renderer.handle_action(
        "RenderingControl",
        "SetMute",
        {"InstanceID": "0", "Channel": "Master", "DesiredMute": "1"},
    )
    assert fake_sonos.mute is True
    assert (
        await renderer.handle_action(
            "RenderingControl", "GetMute", {"InstanceID": "0", "Channel": "Master"}
        )
    )["CurrentMute"] == "1"


async def test_volume_is_clamped(renderer, fake_sonos):
    await renderer.handle_action(
        "RenderingControl",
        "SetVolume",
        {"InstanceID": "0", "Channel": "Master", "DesiredVolume": "150"},
    )
    assert fake_sonos.volume == 100


# ----------------------------------------------------------------------
# Grouped rooms
# ----------------------------------------------------------------------
async def test_transport_goes_to_the_coordinator_but_volume_stays_local(config, fake_sonos):
    slave = ZoneInfo(
        uid="RINCON_BBB01400",
        name="Bedroom",
        ip="192.168.1.20",
        coordinator_uid="RINCON_AAA01400",
    )
    master = ZoneInfo(
        uid="RINCON_AAA01400",
        name="Kitchen",
        ip="192.168.1.10",
        coordinator_uid="RINCON_AAA01400",
    )
    topology = StubTopology({slave.uid: slave, master.uid: master})
    renderer = VirtualRenderer(config, slave, topology, fake_sonos, "http://192.168.1.2:1500")

    await set_uri(renderer, TRACK1)
    await renderer.handle_action(
        "RenderingControl",
        "SetVolume",
        {"InstanceID": "0", "Channel": "Master", "DesiredVolume": "30"},
    )

    assert "192.168.1.10" in fake_sonos.urls_for("AddURIToQueue")[0]
    # The queue belongs to the coordinator, so its UID must appear in the URI.
    assert fake_sonos.av_transport_uri == "x-rincon-queue:RINCON_AAA01400#0"
    assert "192.168.1.20" in fake_sonos.urls_for("SetVolume")[0]


async def test_ungroup_on_play_detaches_the_room_first(zone, fake_sonos):
    slave = ZoneInfo(
        uid="RINCON_BBB01400",
        name="Bedroom",
        ip="192.168.1.20",
        coordinator_uid="RINCON_AAA01400",
    )
    topology = StubTopology({slave.uid: slave, zone.uid: zone})
    renderer = VirtualRenderer(
        Config(mode="queue", ungroup_on_play=True),
        slave,
        topology,
        fake_sonos,
        "http://192.168.1.2:1500",
    )
    await set_uri(renderer, TRACK1)
    assert "BecomeCoordinatorOfStandaloneGroup" in fake_sonos.actions()


# ----------------------------------------------------------------------
# Protocol info and misc actions
# ----------------------------------------------------------------------
async def test_protocol_info_is_http_audio_only(renderer):
    result = await renderer.handle_action("ConnectionManager", "GetProtocolInfo", {})
    entries = result["Sink"].split(",")
    assert "http-get:*:audio/flac:*" in entries
    assert all(entry.startswith("http-get:") for entry in entries)
    assert not any("x-rincon" in entry for entry in entries)
    assert result["Source"] == ""


async def test_seek_modes(renderer, fake_sonos):
    await renderer.handle_action(
        "AVTransport", "Seek", {"InstanceID": "0", "Unit": "REL_TIME", "Target": "0:01:00"}
    )
    assert fake_sonos.args_for("Seek")["Target"] == "0:01:00"

    await renderer.handle_action(
        "AVTransport", "Seek", {"InstanceID": "0", "Unit": "TRACK_NR", "Target": "1"}
    )

    with pytest.raises(UPnPError) as excinfo:
        await renderer.handle_action(
            "AVTransport", "Seek", {"InstanceID": "0", "Unit": "X_DLNA_REL_BYTE", "Target": "1"}
        )
    assert excinfo.value.code == 710


async def test_unknown_instance_is_rejected(renderer):
    with pytest.raises(UPnPError) as excinfo:
        await renderer.handle_action("AVTransport", "GetPositionInfo", {"InstanceID": "7"})
    assert excinfo.value.code == 718


async def test_unknown_action_is_rejected(renderer):
    with pytest.raises(UPnPError) as excinfo:
        await renderer.handle_action("AVTransport", "Record", {"InstanceID": "0"})
    assert excinfo.value.code == 401


async def test_player_errors_surface_as_upnp_errors(renderer, fake_sonos):
    fake_sonos.errors["Play"] = UPnPError(701, "Transition not available")
    with pytest.raises(UPnPError) as excinfo:
        await renderer.handle_action("AVTransport", "Play", {"InstanceID": "0", "Speed": "1"})
    assert excinfo.value.code == 701


# ----------------------------------------------------------------------
# Direct mode
# ----------------------------------------------------------------------
async def test_direct_mode_sets_the_uri_without_touching_the_queue(zone, fake_sonos):
    renderer = VirtualRenderer(
        Config(mode="direct"),
        zone,
        StubTopology({zone.uid: zone}),
        fake_sonos,
        "http://192.168.1.2:1500",
    )
    await set_uri(renderer, TRACK1)
    assert "RemoveAllTracksFromQueue" not in fake_sonos.actions()
    assert "AddURIToQueue" not in fake_sonos.actions()
    assert fake_sonos.args_for("SetAVTransportURI")["CurrentURI"] == TRACK1
    assert "RINCON_AssociatedZPUDN" in fake_sonos.args_for("SetAVTransportURI")["CurrentURIMetaData"]


async def test_direct_mode_advances_itself_at_end_of_track(zone, fake_sonos):
    renderer = VirtualRenderer(
        Config(mode="direct"),
        zone,
        StubTopology({zone.uid: zone}),
        fake_sonos,
        "http://192.168.1.2:1500",
    )
    await set_uri(renderer, TRACK1)
    await renderer.handle_action("AVTransport", "Play", {"InstanceID": "0", "Speed": "1"})
    # Sonos rejects SetNextAVTransportURI outside queue playback; the bridge
    # remembers the track and loads it itself.
    await set_next(renderer, TRACK2)
    assert renderer.state.next_uri == TRACK2

    fake_sonos.position = fake_sonos.duration  # the track ran to the end
    renderer.state.transport_state = "PLAYING"
    await renderer.on_sonos_avtransport({"TransportState": "STOPPED"})
    await _drain()

    assert renderer.state.current_uri == TRACK2
    assert fake_sonos.transport_state == "PLAYING"


async def test_direct_mode_does_not_advance_on_a_user_stop(zone, fake_sonos):
    renderer = VirtualRenderer(
        Config(mode="direct"),
        zone,
        StubTopology({zone.uid: zone}),
        fake_sonos,
        "http://192.168.1.2:1500",
    )
    await set_uri(renderer, TRACK1)
    await set_next(renderer, TRACK2)
    fake_sonos.position = "0:00:30"  # stopped in the middle
    renderer.state.transport_state = "PLAYING"

    await renderer.on_sonos_avtransport({"TransportState": "STOPPED"})
    await _drain()

    assert renderer.state.current_uri == TRACK1


# ----------------------------------------------------------------------
# Events
# ----------------------------------------------------------------------
class RecordingSubscriptions:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __len__(self):
        return 0

    async def notify(self, service_id, properties):
        self.events.append((service_id, dict(properties)))


async def test_player_state_changes_are_forwarded_to_subscribers(renderer):
    recorder = RecordingSubscriptions()
    renderer.subscriptions = recorder

    await renderer.on_sonos_avtransport({"TransportState": "PLAYING"})
    await renderer.on_sonos_renderingcontrol({"Volume/Master": "33"})

    services = [service for service, _ in recorder.events]
    assert services == ["AVTransport", "RenderingControl"]
    assert "PLAYING" in recorder.events[0][1]["LastChange"]
    assert 'val="33"' in recorder.events[1][1]["LastChange"]


async def test_unchanged_state_does_not_spam_subscribers(renderer):
    recorder = RecordingSubscriptions()
    renderer.subscriptions = recorder
    renderer.state.transport_state = "PLAYING"

    await renderer.on_sonos_avtransport({"TransportState": "PLAYING"})

    assert recorder.events == []


async def test_track_change_events_move_the_bridge_forward(renderer):
    await set_uri(renderer, TRACK1)
    await set_next(renderer, TRACK2)
    recorder = RecordingSubscriptions()
    renderer.subscriptions = recorder

    await renderer.on_sonos_avtransport({"CurrentTrackURI": TRACK2})

    assert renderer.state.current_uri == TRACK2
    assert renderer.state.next_uri == ""
    assert TRACK2 in recorder.events[0][1]["LastChange"]


async def _drain() -> None:
    """Let tasks spawned by event handlers finish."""
    import asyncio

    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=2)


async def test_replacing_the_next_track_after_sonos_advanced_keeps_playing(
    renderer, fake_sonos
):
    """Once Sonos moves into the queue, the track it is playing is no longer
    queue entry 1 - trimming must start after the current position, not after
    the first entry."""
    await set_uri(renderer, TRACK1)
    await renderer.handle_action("AVTransport", "Play", {"InstanceID": "0", "Speed": "1"})
    await set_next(renderer, TRACK2)

    fake_sonos.advance()
    await renderer.handle_action("AVTransport", "GetPositionInfo", {"InstanceID": "0"})
    assert renderer.state.queue_index == 2

    await set_next(renderer, TRACK3)
    await set_next(renderer, "http://192.168.1.5:52341/4.flac")

    uris = [uri for uri, _ in fake_sonos.queue]
    assert uris == [TRACK1, TRACK2, "http://192.168.1.5:52341/4.flac"]
    # The track Sonos is playing right now must still be under the play head.
    assert fake_sonos.current_uri() == TRACK2


async def test_queue_position_is_reset_by_a_fresh_load(renderer, fake_sonos):
    await set_uri(renderer, TRACK1)
    await set_next(renderer, TRACK2)
    fake_sonos.advance()
    await renderer.handle_action("AVTransport", "GetPositionInfo", {"InstanceID": "0"})
    assert renderer.state.queue_index == 2

    renderer.state.transport_state = "STOPPED"
    await set_uri(renderer, TRACK3)

    assert renderer.state.queue_index == 1
    assert [uri for uri, _ in fake_sonos.queue] == [TRACK3]
