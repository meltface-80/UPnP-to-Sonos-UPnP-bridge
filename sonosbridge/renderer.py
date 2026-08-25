"""The virtual MediaRenderer: one per Sonos room.

This is where the actual bridging happens.  Audirvana talks plain
``MediaRenderer:1`` to this object; the object talks Sonos dialect to the real
player - queue-based loading for gapless playback, rebuilt DIDL-Lite metadata,
and transport commands routed to whichever player currently coordinates the
room's group.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from xml.sax.saxutils import escape

from . import didl, lastchange, protocolinfo, timeutil
from .config import BRIDGE_NAME, BRIDGE_VERSION, Config
from .icon import icon_list_xml
from .soap import UPnPError
from .sonos import (
    AV_TRANSPORT,
    CONNECTION_MANAGER,
    RENDERING_CONTROL,
    SonosPlayer,
    ZoneInfo,
)

LOGGER = logging.getLogger(__name__)

MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"

SERVICE_IDS = {
    AV_TRANSPORT: "AVTransport",
    RENDERING_CONTROL: "RenderingControl",
    CONNECTION_MANAGER: "ConnectionManager",
}
SERVICE_TYPES = {name: urn for urn, name in SERVICE_IDS.items()}

# States in which a repeated SetAVTransportURI for the track that is already
# loaded must not restart the transport.
ACTIVE_STATES = frozenset({"PLAYING", "TRANSITIONING", "PAUSED_PLAYBACK"})

TRANSPORT_ACTIONS = "Play,Stop,Pause,Seek,Next,Previous,X_DLNA_SeekTime"

# How close to the end of a track counts as "the track finished" when the bridge
# has to detect track ends itself (direct mode only).
END_OF_TRACK_SLACK = 4.0


@dataclass
class RendererState:
    """Everything the bridge reports back to the control point."""

    transport_state: str = "STOPPED"
    transport_status: str = "OK"
    play_mode: str = "NORMAL"
    current_uri: str = ""
    current_meta: str = ""
    next_uri: str = ""
    next_meta: str = ""
    track_duration: str = "0:00:00"
    rel_time: str = "0:00:00"
    volume: int = 0
    mute: bool = False
    # Sonos keeps playing through its queue, so the track the control point
    # thinks of as "current" drifts to a higher queue position as it advances.
    # Both numbers are needed to trim the queue without cutting off playback.
    queue_index: int = 1
    queue_length: int = 0


class VirtualRenderer:
    """A standards-clean UPnP MediaRenderer that proxies one Sonos room."""

    def __init__(
        self,
        config: Config,
        zone: ZoneInfo,
        topology,
        soap_client,
        base_url: str,
    ) -> None:
        self.config = config
        self.zone = zone
        self.topology = topology
        self._soap = soap_client
        self.base_url = base_url.rstrip("/")

        self.uuid = config.udn_for(zone.uid).removeprefix("uuid:")
        self.udn = f"uuid:{self.uuid}"
        self.state = RendererState()
        self.subscriptions = None  # set by the HTTP layer
        self._sink_protocol_info = ""
        self._lock = asyncio.Lock()
        # SIDs of the subscriptions the bridge holds on the real player; event
        # callbacks that do not carry one of these are not from our player.
        self.sonos_sids: set[str] = set()
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    @property
    def friendly_name(self) -> str:
        return f"{self.zone.name}{self.config.name_suffix}"

    @property
    def device_path(self) -> str:
        return f"/dev/{self.uuid}"

    @property
    def description_url(self) -> str:
        return f"{self.base_url}{self.device_path}/desc.xml"

    def _spawn(self, coro, name: str) -> asyncio.Task:
        """Run *coro* detached, holding a reference so it is not collected."""
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def update_zone(self, zone: ZoneInfo) -> None:
        """Adopt refreshed topology data (IP change, rename, regrouping)."""
        self.zone = zone

    # ------------------------------------------------------------------
    # Sonos player handles
    # ------------------------------------------------------------------
    def player(self) -> SonosPlayer:
        """The room's own player - the target for volume and mute."""
        return SonosPlayer(self.zone.ip, self._soap, self.zone.uid, self.zone.name)

    def coordinator(self) -> SonosPlayer:
        """The player that owns the queue for this room's group."""
        zone = self.topology.coordinator_for(self.zone.uid) if self.topology else None
        if zone is None:
            zone = self.zone
        return SonosPlayer(zone.ip, self._soap, zone.uid, zone.name)

    # ------------------------------------------------------------------
    # Device / service descriptions
    # ------------------------------------------------------------------
    def description_xml(self) -> str:
        services = []
        for service_type, name in SERVICE_IDS.items():
            services.append(
                "<service>"
                f"<serviceType>{service_type}</serviceType>"
                f"<serviceId>urn:upnp-org:serviceId:{name}</serviceId>"
                f"<SCPDURL>/scpd/{name}.xml</SCPDURL>"
                f"<controlURL>{self.device_path}/svc/{name}/control</controlURL>"
                f"<eventSubURL>{self.device_path}/svc/{name}/event</eventSubURL>"
                "</service>"
            )
        model = self.zone.model or "Sonos Player"
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<root xmlns="urn:schemas-upnp-org:device-1-0"'
            ' xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion>"
            f"<URLBase>{escape(self.base_url)}/</URLBase>"
            "<device>"
            "<dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>"
            f"<deviceType>{MEDIA_RENDERER}</deviceType>"
            f"<friendlyName>{escape(self.friendly_name)}</friendlyName>"
            "<manufacturer>Sonos, Inc.</manufacturer>"
            "<manufacturerURL>https://www.sonos.com</manufacturerURL>"
            f"<modelDescription>{escape(self.zone.name)} bridged by "
            f"{BRIDGE_NAME} {BRIDGE_VERSION}</modelDescription>"
            f"<modelName>{escape(model)}</modelName>"
            "<modelURL>https://github.com/meltface-80/"
            "UPnP-to-Sonos-UPnP-bridge-for-Audirvana-</modelURL>"
            f"<serialNumber>{escape(self.zone.uid)}</serialNumber>"
            f"<UDN>{self.udn}</UDN>"
            f"{icon_list_xml(self.device_path)}"
            f"<serviceList>{''.join(services)}</serviceList>"
            f"<presentationURL>{escape(self.base_url)}/</presentationURL>"
            "</device></root>"
        )

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------
    async def handle_action(
        self, service_name: str, action: str, args: dict[str, str]
    ) -> dict[str, str]:
        handlers = {
            "AVTransport": self._avtransport,
            "RenderingControl": self._rendering_control,
            "ConnectionManager": self._connection_manager,
        }
        handler = handlers.get(service_name)
        if handler is None:
            raise UPnPError(401, f"Unknown service {service_name}")

        instance = args.get("InstanceID", "0").strip() or "0"
        if service_name != "ConnectionManager" and instance not in ("0", ""):
            raise UPnPError(718, "Invalid InstanceID")
        return await handler(action, args)

    # ------------------------------------------------------------------
    # AVTransport
    # ------------------------------------------------------------------
    async def _avtransport(self, action: str, args: dict[str, str]) -> dict[str, str]:
        if action == "SetAVTransportURI":
            return await self._set_av_transport_uri(
                args.get("CurrentURI", ""), args.get("CurrentURIMetaData", "")
            )
        if action == "SetNextAVTransportURI":
            return await self._set_next_av_transport_uri(
                args.get("NextURI", ""), args.get("NextURIMetaData", "")
            )
        if action == "Play":
            await self.coordinator().play()
            self.state.transport_state = "PLAYING"
            await self.publish_transport_event()
            return {}
        if action == "Pause":
            await self.coordinator().pause()
            self.state.transport_state = "PAUSED_PLAYBACK"
            await self.publish_transport_event()
            return {}
        if action == "Stop":
            await self.coordinator().stop()
            self.state.transport_state = "STOPPED"
            await self.publish_transport_event()
            return {}
        if action == "Next":
            await self.coordinator().next_track()
            return {}
        if action == "Previous":
            await self.coordinator().previous_track()
            return {}
        if action == "Seek":
            return await self._seek(args.get("Unit", ""), args.get("Target", ""))
        if action == "SetPlayMode":
            return await self._set_play_mode(args.get("NewPlayMode", "NORMAL"))
        if action == "GetTransportInfo":
            return await self._get_transport_info()
        if action == "GetPositionInfo":
            return await self._get_position_info()
        if action == "GetMediaInfo":
            return self._get_media_info()
        if action == "GetTransportSettings":
            return {
                "PlayMode": self.state.play_mode,
                "RecQualityMode": "NOT_IMPLEMENTED",
            }
        if action == "GetDeviceCapabilities":
            return {
                "PlayMedia": "NONE,NETWORK",
                "RecMedia": "NOT_IMPLEMENTED",
                "RecQualityModes": "NOT_IMPLEMENTED",
            }
        if action == "GetCurrentTransportActions":
            return {"Actions": TRANSPORT_ACTIONS}
        raise UPnPError(401, f"Unsupported AVTransport action {action}")

    async def _set_av_transport_uri(self, uri: str, metadata: str) -> dict[str, str]:
        uri = (uri or "").strip()
        if not uri:
            await self._clear()
            return {}

        state = self.state
        # Audirvana re-sends the current track when it confirms a track change,
        # and re-sends the track it already queued as "next" once Sonos moves on.
        # Reloading either would restart playback and break gapless, so both are
        # treated as metadata-only updates.
        if uri == state.current_uri and state.transport_state in ACTIVE_STATES:
            state.current_meta = metadata or state.current_meta
            return {}
        if (
            uri == state.next_uri
            and state.next_uri
            and state.transport_state in ACTIVE_STATES
        ):
            state.current_uri, state.current_meta = uri, metadata or state.next_meta
            state.next_uri, state.next_meta = "", ""
            state.queue_index = min(state.queue_index + 1, max(1, state.queue_length))
            await self.publish_transport_event()
            return {}

        async with self._lock:
            await self._load(uri, metadata)
        await self.publish_transport_event()
        return {}

    async def _load(self, uri: str, metadata: str) -> None:
        """Put *uri* in front of the player, ready to play."""
        state = self.state
        payload = didl.rebuild(uri, metadata)

        if self.config.ungroup_on_play and not self.zone.is_coordinator:
            try:
                await self.player().become_standalone()
                if self.topology:
                    await self.topology.refresh()
            except UPnPError as exc:
                LOGGER.debug("%s: could not ungroup: %s", self.zone.name, exc)

        player = self.coordinator()
        if self.config.mode == "queue":
            await player.clear_queue()
            result = await player.add_uri_to_queue(uri, payload, desired_first_track=1)
            state.queue_length = _as_int(result.get("NewQueueLength"), 1)
            state.queue_index = 1
            await player.set_av_transport_uri(player.queue_uri(), "")
            await player.seek("TRACK_NR", "1")
        else:
            await player.set_av_transport_uri(uri, payload)
            state.queue_length = 0
            state.queue_index = 1

        # A player left in shuffle or repeat would otherwise ignore the order
        # Audirvana is feeding it.
        try:
            await player.set_play_mode("NORMAL")
            state.play_mode = "NORMAL"
        except UPnPError as exc:
            LOGGER.debug("%s: SetPlayMode failed: %s", self.zone.name, exc)

        state.current_uri, state.current_meta = uri, metadata
        state.next_uri, state.next_meta = "", ""
        parsed = didl.parse(metadata)
        state.track_duration = parsed.duration or "0:00:00"
        state.rel_time = "0:00:00"
        LOGGER.info("%s: loaded %s", self.zone.name, uri)

    async def _set_next_av_transport_uri(self, uri: str, metadata: str) -> dict[str, str]:
        uri = (uri or "").strip()
        state = self.state

        async with self._lock:
            if self.config.mode != "queue":
                state.next_uri, state.next_meta = uri, metadata
                if uri:
                    try:
                        await self.coordinator().set_next_av_transport_uri(
                            uri, didl.rebuild(uri, metadata)
                        )
                    except UPnPError as exc:
                        # Expected on Sonos outside queue playback; the transport
                        # watcher loads the track itself when the current one ends.
                        LOGGER.debug(
                            "%s: SetNextAVTransportURI not accepted (%s); will "
                            "advance manually",
                            self.zone.name,
                            exc,
                        )
                return {}

            player = self.coordinator()
            if state.queue_length > state.queue_index:
                # Drop whatever was queued after the current track. Trimming from
                # index 1 would remove the track that is playing right now.
                try:
                    await player.remove_track_range_from_queue(
                        state.queue_index + 1, state.queue_length - state.queue_index
                    )
                except UPnPError as exc:
                    LOGGER.debug("%s: could not trim queue: %s", self.zone.name, exc)
                state.queue_length = state.queue_index

            if not uri:
                state.next_uri, state.next_meta = "", ""
                return {}

            result = await player.add_uri_to_queue(uri, didl.rebuild(uri, metadata))
            state.queue_length = _as_int(
                result.get("NewQueueLength"), state.queue_index + 1
            )
            state.next_uri, state.next_meta = uri, metadata
            LOGGER.debug("%s: queued next %s", self.zone.name, uri)
        await self.publish_transport_event()
        return {}

    async def _clear(self) -> None:
        state = self.state
        player = self.coordinator()
        try:
            await player.stop()
        except UPnPError:
            pass
        if self.config.mode == "queue":
            try:
                await player.clear_queue()
            except UPnPError:
                pass
        state.current_uri = state.current_meta = ""
        state.next_uri = state.next_meta = ""
        state.queue_length = 0
        state.transport_state = "STOPPED"
        state.track_duration = "0:00:00"
        state.rel_time = "0:00:00"
        await self.publish_transport_event()

    async def _seek(self, unit: str, target: str) -> dict[str, str]:
        unit = (unit or "").upper()
        if unit in ("REL_TIME", "ABS_TIME", "X_DLNA_REL_TIME"):
            await self.coordinator().seek("REL_TIME", target)
            return {}
        if unit == "TRACK_NR":
            # The bridge presents a single-track transport, so seeking to track 1
            # is a no-op and anything else is out of range.
            if target.strip() in ("", "0", "1"):
                return {}
            raise UPnPError(711, "Illegal seek target")
        raise UPnPError(710, "Seek mode not supported")

    async def _set_play_mode(self, mode: str) -> dict[str, str]:
        mode = (mode or "NORMAL").upper()
        try:
            await self.coordinator().set_play_mode(mode)
        except UPnPError as exc:
            LOGGER.debug("%s: SetPlayMode(%s) rejected: %s", self.zone.name, mode, exc)
            raise
        self.state.play_mode = mode
        await self.publish_transport_event()
        return {}

    async def _get_transport_info(self) -> dict[str, str]:
        info = await self.coordinator().get_transport_info()
        state = self.state
        state.transport_state = info.get("CurrentTransportState", state.transport_state)
        state.transport_status = info.get("CurrentTransportStatus", "OK")
        return {
            "CurrentTransportState": state.transport_state,
            "CurrentTransportStatus": state.transport_status,
            "CurrentSpeed": info.get("CurrentSpeed", "1") or "1",
        }

    async def _get_position_info(self) -> dict[str, str]:
        info = await self.coordinator().get_position_info()
        state = self.state
        self._reconcile_track(info.get("TrackURI", ""), info.get("Track", ""))
        duration = info.get("TrackDuration", "") or state.track_duration
        state.track_duration = duration
        state.rel_time = info.get("RelTime", "0:00:00") or "0:00:00"
        return {
            "Track": "1" if state.current_uri else "0",
            "TrackDuration": duration,
            "TrackMetaData": state.current_meta or info.get("TrackMetaData", ""),
            "TrackURI": state.current_uri or info.get("TrackURI", ""),
            "RelTime": state.rel_time,
            "AbsTime": info.get("AbsTime", "NOT_IMPLEMENTED"),
            "RelCount": info.get("RelCount", "2147483647"),
            "AbsCount": info.get("AbsCount", "2147483647"),
        }

    def _get_media_info(self) -> dict[str, str]:
        state = self.state
        return {
            "NrTracks": "1" if state.current_uri else "0",
            "MediaDuration": state.track_duration,
            "CurrentURI": state.current_uri,
            "CurrentURIMetaData": state.current_meta,
            "NextURI": state.next_uri,
            "NextURIMetaData": state.next_meta,
            "PlayMedium": "NETWORK" if state.current_uri else "NONE",
            "RecordMedium": "NOT_IMPLEMENTED",
            "WriteStatus": "NOT_IMPLEMENTED",
        }

    def _reconcile_track(self, sonos_track_uri: str, track_number: str = "") -> bool:
        """Notice when Sonos has moved on to the queued 'next' track."""
        state = self.state
        reported = _as_int(track_number, 0)
        if reported > 0:
            # The player is authoritative about where it is in its own queue.
            state.queue_index = reported
        if not sonos_track_uri or not state.next_uri:
            return False
        if sonos_track_uri != state.next_uri:
            return False
        LOGGER.debug("%s: advanced to queued next track", self.zone.name)
        state.current_uri, state.current_meta = state.next_uri, state.next_meta
        state.next_uri, state.next_meta = "", ""
        if reported <= 0:
            state.queue_index = min(state.queue_index + 1, max(1, state.queue_length))
        parsed = didl.parse(state.current_meta)
        if parsed.duration:
            state.track_duration = parsed.duration
        return True

    # ------------------------------------------------------------------
    # RenderingControl
    # ------------------------------------------------------------------
    async def _rendering_control(self, action: str, args: dict[str, str]) -> dict[str, str]:
        player = self.player()
        if action == "GetVolume":
            volume = await player.get_volume()
            self.state.volume = volume
            return {"CurrentVolume": str(volume)}
        if action == "SetVolume":
            volume = _as_int(args.get("DesiredVolume"), self.state.volume)
            await player.set_volume(volume)
            self.state.volume = max(0, min(100, volume))
            await self.publish_rendering_event()
            return {}
        if action == "GetMute":
            muted = await player.get_mute()
            self.state.mute = muted
            return {"CurrentMute": "1" if muted else "0"}
        if action == "SetMute":
            muted = args.get("DesiredMute", "0") in ("1", "true", "True", "yes")
            await player.set_mute(muted)
            self.state.mute = muted
            await self.publish_rendering_event()
            return {}
        if action == "ListPresets":
            return {"CurrentPresetNameList": "FactoryDefaults"}
        if action == "SelectPreset":
            return {}
        raise UPnPError(401, f"Unsupported RenderingControl action {action}")

    # ------------------------------------------------------------------
    # ConnectionManager
    # ------------------------------------------------------------------
    async def _connection_manager(self, action: str, args: dict[str, str]) -> dict[str, str]:
        if action == "GetProtocolInfo":
            return {"Source": "", "Sink": await self.sink_protocol_info()}
        if action == "GetCurrentConnectionIDs":
            return {"ConnectionIDs": "0"}
        if action == "GetCurrentConnectionInfo":
            return {
                "RcsID": "0",
                "AVTransportID": "0",
                "ProtocolInfo": "",
                "PeerConnectionManager": "",
                "PeerConnectionID": "-1",
                "Direction": "Input",
                "Status": "OK",
            }
        raise UPnPError(401, f"Unsupported ConnectionManager action {action}")

    async def sink_protocol_info(self) -> str:
        if not self._sink_protocol_info:
            sonos_sink = ""
            try:
                result = await self.player().get_protocol_info()
                sonos_sink = result.get("Sink", "")
            except UPnPError as exc:
                LOGGER.debug("%s: GetProtocolInfo failed: %s", self.zone.name, exc)
            self._sink_protocol_info = protocolinfo.build_sink(
                sonos_sink, self.config.extra_protocol_info
            )
        return self._sink_protocol_info

    # ------------------------------------------------------------------
    # Eventing towards the control point
    # ------------------------------------------------------------------
    def avtransport_variables(self) -> dict[str, str]:
        state = self.state
        return {
            "TransportState": state.transport_state,
            "TransportStatus": state.transport_status,
            "PlaybackStorageMedium": "NETWORK" if state.current_uri else "NONE",
            "RecordStorageMedium": "NOT_IMPLEMENTED",
            "CurrentPlayMode": state.play_mode,
            "TransportPlaySpeed": "1",
            "RecordMediumWriteStatus": "NOT_IMPLEMENTED",
            "CurrentRecordQualityMode": "NOT_IMPLEMENTED",
            "PossibleRecordQualityModes": "NOT_IMPLEMENTED",
            "NumberOfTracks": "1" if state.current_uri else "0",
            "CurrentTrack": "1" if state.current_uri else "0",
            "CurrentTrackDuration": state.track_duration,
            "CurrentMediaDuration": state.track_duration,
            "CurrentTrackURI": state.current_uri,
            "CurrentTrackMetaData": state.current_meta,
            "AVTransportURI": state.current_uri,
            "AVTransportURIMetaData": state.current_meta,
            "NextAVTransportURI": state.next_uri,
            "NextAVTransportURIMetaData": state.next_meta,
            "CurrentTransportActions": TRANSPORT_ACTIONS,
        }

    def renderingcontrol_variables(self) -> dict[str, str]:
        return {
            "Volume/Master": str(self.state.volume),
            "Mute/Master": "1" if self.state.mute else "0",
            "PresetNameList": "FactoryDefaults",
        }

    async def publish_transport_event(self) -> None:
        if self.subscriptions is None:
            return
        await self.subscriptions.notify(
            "AVTransport",
            {"LastChange": lastchange.build_avtransport(self.avtransport_variables())},
        )

    async def publish_rendering_event(self) -> None:
        if self.subscriptions is None:
            return
        await self.subscriptions.notify(
            "RenderingControl",
            {
                "LastChange": lastchange.build_rendering_control(
                    self.renderingcontrol_variables()
                )
            },
        )

    def initial_event(self, service_name: str) -> dict[str, str]:
        if service_name == "AVTransport":
            return {
                "LastChange": lastchange.build_avtransport(self.avtransport_variables())
            }
        if service_name == "RenderingControl":
            return {
                "LastChange": lastchange.build_rendering_control(
                    self.renderingcontrol_variables()
                )
            }
        return {
            "SourceProtocolInfo": "",
            "SinkProtocolInfo": self._sink_protocol_info,
            "CurrentConnectionIDs": "0",
        }

    # ------------------------------------------------------------------
    # Eventing from the Sonos player
    # ------------------------------------------------------------------
    async def on_sonos_avtransport(self, values: dict[str, str]) -> None:
        state = self.state
        changed = False

        transport_state = values.get("TransportState")
        if transport_state and transport_state != state.transport_state:
            previous = state.transport_state
            state.transport_state = transport_state
            changed = True
            if transport_state == "STOPPED" and previous in ("PLAYING", "TRANSITIONING"):
                self._spawn(self._maybe_advance(), f"advance-{self.uuid}")

        track_uri = values.get("CurrentTrackURI", "")
        if track_uri and self._reconcile_track(track_uri, values.get("CurrentTrack", "")):
            changed = True

        duration = values.get("CurrentTrackDuration")
        if duration and duration != state.track_duration and duration != "0:00:00":
            state.track_duration = duration
            changed = True

        play_mode = values.get("CurrentPlayMode")
        if play_mode and play_mode != state.play_mode:
            state.play_mode = play_mode
            changed = True

        if changed:
            await self.publish_transport_event()

    async def on_sonos_renderingcontrol(self, values: dict[str, str]) -> None:
        state = self.state
        changed = False
        volume = values.get("Volume/Master")
        if volume is not None:
            parsed = _as_int(volume, state.volume)
            if parsed != state.volume:
                state.volume = parsed
                changed = True
        mute = values.get("Mute/Master")
        if mute is not None:
            parsed_mute = mute in ("1", "true", "True")
            if parsed_mute != state.mute:
                state.mute = parsed_mute
                changed = True
        if changed:
            await self.publish_rendering_event()

    async def _maybe_advance(self) -> None:
        """Direct mode only: load the queued next track when one finishes.

        Queue mode never needs this - Sonos moves through the queue itself - and
        a Stop that the user asked for must not be mistaken for a track ending,
        hence the position check.
        """
        if self.config.mode == "queue" or not self.state.next_uri:
            return
        try:
            info = await self.coordinator().get_position_info()
        except UPnPError:
            return
        position = timeutil.to_seconds(info.get("RelTime", ""))
        duration = timeutil.to_seconds(info.get("TrackDuration", ""))
        finished = position < 0 or duration < 0 or duration - position <= END_OF_TRACK_SLACK
        if not finished:
            return
        next_uri, next_meta = self.state.next_uri, self.state.next_meta
        async with self._lock:
            await self._load(next_uri, next_meta)
            await self.coordinator().play()
            self.state.transport_state = "PLAYING"
        await self.publish_transport_event()

    # ------------------------------------------------------------------
    # Periodic reconciliation (safety net if an event is missed)
    # ------------------------------------------------------------------
    async def refresh(self) -> None:
        try:
            player = self.coordinator()
            transport = await player.get_transport_info()
            position = await player.get_position_info()
            volume = await self.player().get_volume()
        except UPnPError as exc:
            LOGGER.debug("%s: refresh failed: %s", self.zone.name, exc)
            return

        state = self.state
        changed = False
        new_state = transport.get("CurrentTransportState", state.transport_state)
        if new_state != state.transport_state:
            state.transport_state = new_state
            changed = True
        if self._reconcile_track(position.get("TrackURI", ""), position.get("Track", "")):
            changed = True
        duration = position.get("TrackDuration", "")
        if duration and duration != state.track_duration:
            state.track_duration = duration
            changed = True
        state.rel_time = position.get("RelTime", state.rel_time)

        if changed:
            await self.publish_transport_event()
        if volume != state.volume:
            state.volume = volume
            await self.publish_rendering_event()

    def status(self) -> dict[str, object]:
        """A JSON-friendly snapshot, used by the bridge's own status page."""
        coordinator = self.topology.coordinator_for(self.zone.uid) if self.topology else None
        return {
            "room": self.zone.name,
            "friendlyName": self.friendly_name,
            "udn": self.udn,
            "sonosUid": self.zone.uid,
            "sonosIp": self.zone.ip,
            "model": self.zone.model,
            "coordinator": coordinator.name if coordinator else self.zone.name,
            "descriptionUrl": self.description_url,
            "transportState": self.state.transport_state,
            "volume": self.state.volume,
            "mute": self.state.mute,
            "currentUri": self.state.current_uri,
            "nextUri": self.state.next_uri,
            "subscribers": len(self.subscriptions) if self.subscriptions else 0,
        }


def _as_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
