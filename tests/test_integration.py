"""Start the real bridge against a simulated Sonos player and drive it the way
Audirvana would: read the description, load a track, play, adjust volume."""

from __future__ import annotations

import asyncio
import socket

import pytest
from defusedxml import ElementTree as DET

from sonosbridge.bridge import Bridge
from sonosbridge.config import Config
from sonosbridge.soap import build_request, parse_response

from .fake_device import FakeSonosDevice

DEVICE_NS = {"d": "urn:schemas-upnp-org:device-1-0"}
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RCS = "urn:schemas-upnp-org:service:RenderingControl:1"
CM = "urn:schemas-upnp-org:service:ConnectionManager:1"
TRACK = "http://192.168.1.5:52341/track.flac"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def running_bridge():
    device = FakeSonosDevice()
    try:
        await device.start()
    except OSError as exc:  # pragma: no cover - depends on the sandbox
        pytest.skip(f"cannot bind the Sonos port 1400: {exc}")

    config = Config(
        bridge_ip="127.0.0.1",
        http_port=free_port(),
        ssdp_port=free_port(),  # keep multicast out of the way of the test host
        static_hosts=["127.0.0.1"],
        discovery_mx=0,
        discovery_attempts=1,
        discovery_interval=3600,
        topology_interval=3600,
        poll_interval=3600,
    )
    bridge = Bridge(config)
    await bridge.start()
    try:
        yield bridge, device
    finally:
        await bridge.stop()
        await device.stop()


async def soap(session, url, service_type, action, args):
    async with session.post(
        url,
        data=build_request(service_type, action, args).encode("utf-8"),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service_type}#{action}"',
        },
    ) as response:
        return parse_response(await response.text(), action)


async def test_every_visible_room_becomes_a_upnp_renderer(running_bridge):
    bridge, _ = running_bridge
    names = sorted(r.friendly_name for r in bridge.renderers.values())
    # Two rooms; the bonded sub in the second group is not a room of its own.
    assert names == ["Kitchen (Sonos)", "Study (Sonos)"]


async def test_a_control_point_can_read_the_description(running_bridge):
    bridge, _ = running_bridge
    import aiohttp

    renderer = next(iter(bridge.renderers.values()))
    async with aiohttp.ClientSession() as session:
        async with session.get(renderer.description_url) as response:
            assert response.status == 200
            root = DET.fromstring(await response.text())

    device = root.find("d:device", DEVICE_NS)
    assert device.find("d:deviceType", DEVICE_NS).text.endswith("MediaRenderer:1")
    assert device.find("d:modelName", DEVICE_NS).text == "Sonos Five"


async def test_playing_a_track_end_to_end(running_bridge):
    bridge, device = running_bridge
    import aiohttp

    renderer = bridge.renderers[
        next(r.uuid for r in bridge.renderers.values() if r.zone.name == "Kitchen")
    ]
    control = f"{bridge.base_url}{renderer.device_path}/svc/AVTransport/control"
    rendering = f"{bridge.base_url}{renderer.device_path}/svc/RenderingControl/control"

    async with aiohttp.ClientSession() as session:
        await soap(session, control, AVT, "SetAVTransportURI", {
            "InstanceID": 0,
            "CurrentURI": TRACK,
            "CurrentURIMetaData": (
                '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<item><dc:title>Test Track</dc:title></item></DIDL-Lite>"
            ),
        })
        await soap(session, control, AVT, "Play", {"InstanceID": 0, "Speed": "1"})

        transport = await soap(session, control, AVT, "GetTransportInfo", {"InstanceID": 0})
        position = await soap(session, control, AVT, "GetPositionInfo", {"InstanceID": 0})
        await soap(session, rendering, RCS, "SetVolume", {
            "InstanceID": 0, "Channel": "Master", "DesiredVolume": 55,
        })
        volume = await soap(session, rendering, RCS, "GetVolume", {
            "InstanceID": 0, "Channel": "Master",
        })

    assert device.player.queue[0][0] == TRACK
    assert "Test Track" in device.player.queue[0][1]
    assert device.player.transport_state == "PLAYING"
    assert transport["CurrentTransportState"] == "PLAYING"
    assert position["TrackURI"] == TRACK
    assert volume["CurrentVolume"] == "55"
    assert device.player.volume == 55


async def test_protocol_info_is_served_from_the_player(running_bridge):
    bridge, _ = running_bridge
    import aiohttp

    renderer = next(iter(bridge.renderers.values()))
    url = f"{bridge.base_url}{renderer.device_path}/svc/ConnectionManager/control"
    async with aiohttp.ClientSession() as session:
        result = await soap(session, url, CM, "GetProtocolInfo", {})
    assert "http-get:*:audio/flac:*" in result["Sink"].split(",")


async def test_the_bridge_subscribes_to_the_player(running_bridge):
    bridge, device = running_bridge
    for _ in range(20):
        if len(device.subscriptions) >= 2 * len(bridge.renderers):
            break
        await asyncio.sleep(0.05)
    # One AVTransport + one RenderingControl subscription per bridged room.
    assert len(device.subscriptions) == 2 * len(bridge.renderers)
    assert all("/sonos/" in callback for callback in device.subscriptions)


async def test_status_reports_the_bridged_rooms(running_bridge):
    bridge, _ = running_bridge
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{bridge.base_url}/status.json") as response:
            status = await response.json()
    assert {device["room"] for device in status["devices"]} == {"Kitchen", "Study"}
    assert status["mode"] == "queue"


async def test_a_control_point_finds_the_bridge_over_ssdp(running_bridge):
    """The step that makes Audirvana see anything at all: an M-SEARCH for
    MediaRenderer must come back with one reply per bridged room."""
    from sonosbridge.discovery import msearch

    bridge, _ = running_bridge
    replies = await msearch(
        "127.0.0.1",
        "urn:schemas-upnp-org:device:MediaRenderer:1",
        mx=1,
        attempts=3,
        port=bridge.config.ssdp_port,
    )

    locations = {headers.get("location", "") for headers, _ in replies}
    expected = {renderer.description_url for renderer in bridge.renderers.values()}
    assert expected <= locations, f"missing SSDP replies for {expected - locations}"

    for headers, _ in replies:
        assert headers[""].upper().startswith("HTTP/1.1 200")
        assert headers["st"] == "urn:schemas-upnp-org:device:MediaRenderer:1"
        assert headers["usn"].startswith("uuid:")
        assert "max-age=" in headers["cache-control"]
        assert headers["ext"] == ""


async def test_a_root_device_search_also_finds_the_bridge(running_bridge):
    from sonosbridge.discovery import msearch

    bridge, _ = running_bridge
    replies = await msearch(
        "127.0.0.1", "upnp:rootdevice", mx=1, attempts=3, port=bridge.config.ssdp_port
    )
    usns = {headers.get("usn", "") for headers, _ in replies}
    for renderer in bridge.renderers.values():
        assert f"{renderer.udn}::upnp:rootdevice" in usns
