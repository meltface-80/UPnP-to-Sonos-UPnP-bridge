"""End-to-end checks over real HTTP: descriptions, SOAP control, GENA."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from defusedxml import ElementTree as DET

from sonosbridge import lastchange
from sonosbridge.gena import SubscriptionManager
from sonosbridge.renderer import VirtualRenderer
from sonosbridge.server import create_app
from sonosbridge.soap import build_request, parse_response

from .conftest import StubTopology

DEVICE_NS = {"d": "urn:schemas-upnp-org:device-1-0"}
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RCS = "urn:schemas-upnp-org:service:RenderingControl:1"


class FakeBridge:
    def __init__(self, renderer: VirtualRenderer) -> None:
        self.renderers = {renderer.uuid: renderer}

    def renderer_for(self, uuid: str):
        return self.renderers.get(uuid)

    def status(self):
        return {
            "name": "Sonos UPnP Bridge",
            "version": "test",
            "bridgeIp": "192.168.1.2",
            "httpPort": 1500,
            "mode": "queue",
            "devices": [r.status() for r in self.renderers.values()],
        }


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client_session:
        yield client_session


@pytest.fixture
async def bridged(config, zone, fake_sonos, session):
    renderer = VirtualRenderer(
        config, zone, StubTopology({zone.uid: zone}), fake_sonos, "http://192.168.1.2:1500"
    )
    renderer.subscriptions = SubscriptionManager(session, 1800)
    client = TestClient(TestServer(create_app(FakeBridge(renderer))))
    await client.start_server()
    try:
        yield client, renderer
    finally:
        await client.close()


async def soap(client, uuid, service, action, args, service_type=AVT):
    return await client.post(
        f"/dev/{uuid}/svc/{service}/control",
        data=build_request(service_type, action, args).encode("utf-8"),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service_type}#{action}"',
        },
    )


# ----------------------------------------------------------------------
# Descriptions
# ----------------------------------------------------------------------
async def test_device_description_is_a_standard_media_renderer(bridged):
    client, renderer = bridged
    response = await client.get(f"/dev/{renderer.uuid}/desc.xml")
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("text/xml")

    root = DET.fromstring(await response.text())
    device = root.find("d:device", DEVICE_NS)
    assert device.find("d:deviceType", DEVICE_NS).text == (
        "urn:schemas-upnp-org:device:MediaRenderer:1"
    )
    assert device.find("d:friendlyName", DEVICE_NS).text == "Kitchen (Sonos)"
    assert device.find("d:UDN", DEVICE_NS).text == renderer.udn

    services = {
        service.find("d:serviceType", DEVICE_NS).text
        for service in device.iter(f"{{{DEVICE_NS['d']}}}service")
    }
    assert services == {
        AVT,
        RCS,
        "urn:schemas-upnp-org:service:ConnectionManager:1",
    }


async def test_the_room_name_is_what_audirvana_will_show(bridged):
    _, renderer = bridged
    assert renderer.friendly_name == "Kitchen (Sonos)"
    renderer.config.name_suffix = ""
    assert renderer.friendly_name == "Kitchen"


async def test_scpds_are_served_and_advertise_the_expected_actions(bridged):
    client, _ = bridged
    response = await client.get("/scpd/AVTransport.xml")
    assert response.status == 200
    body = await response.text()
    for action in ("SetAVTransportURI", "SetNextAVTransportURI", "Play", "Seek"):
        assert f"<name>{action}</name>" in body

    assert (await client.get("/scpd/Nonsense.xml")).status == 404


async def test_icons_are_served(bridged):
    client, renderer = bridged
    response = await client.get(f"/dev/{renderer.uuid}/icon/48.png")
    assert response.status == 200
    assert (await response.read())[:8] == b"\x89PNG\r\n\x1a\n"
    assert (await client.get(f"/dev/{renderer.uuid}/icon/999.png")).status == 404


async def test_the_icon_matches_the_room_hardware(bridged):
    client, renderer = bridged
    assert renderer.icon_kind == "one"  # the fixture room is a Sonos One
    one = await (await client.get(f"/dev/{renderer.uuid}/icon/48.png")).read()

    renderer.zone.model = "Sonos Beam"
    beam = await (await client.get(f"/dev/{renderer.uuid}/icon/48.png")).read()
    assert beam != one

    renderer.zone.channel_map = "RINCON_AAA01400:LF,LF;RINCON_BBB01400:RF,RF"
    pair = await (await client.get(f"/dev/{renderer.uuid}/icon/48.png")).read()
    assert pair != beam


async def test_the_icon_is_also_available_as_svg(bridged):
    client, renderer = bridged
    response = await client.get(f"/dev/{renderer.uuid}/icon.svg")
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("image/svg+xml")
    body = await response.text()
    assert body.startswith("<svg") and "<title>Sonos One</title>" in body


async def test_unknown_device_is_a_404(bridged):
    client, _ = bridged
    assert (await client.get("/dev/not-a-device/desc.xml")).status == 404


# ----------------------------------------------------------------------
# SOAP control
# ----------------------------------------------------------------------
async def test_soap_control_round_trip(bridged, fake_sonos):
    client, renderer = bridged
    response = await soap(
        client,
        renderer.uuid,
        "RenderingControl",
        "SetVolume",
        {"InstanceID": 0, "Channel": "Master", "DesiredVolume": 35},
        RCS,
    )
    assert response.status == 200
    parse_response(await response.text(), "SetVolume")
    assert fake_sonos.volume == 35

    response = await soap(
        client,
        renderer.uuid,
        "RenderingControl",
        "GetVolume",
        {"InstanceID": 0, "Channel": "Master"},
        RCS,
    )
    assert parse_response(await response.text(), "GetVolume")["CurrentVolume"] == "35"


async def test_set_uri_over_soap_reaches_the_player(bridged, fake_sonos):
    client, renderer = bridged
    uri = "http://192.168.1.5:52341/a%20b.flac"
    metadata = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<item><dc:title>A &amp; B</dc:title></item></DIDL-Lite>"
    )
    response = await soap(
        client,
        renderer.uuid,
        "AVTransport",
        "SetAVTransportURI",
        {"InstanceID": 0, "CurrentURI": uri, "CurrentURIMetaData": metadata},
    )
    assert response.status == 200
    assert fake_sonos.queue[0][0] == uri
    assert "A &amp; B" in fake_sonos.queue[0][1]


async def test_failures_come_back_as_upnp_faults(bridged):
    client, renderer = bridged
    response = await soap(
        client, renderer.uuid, "AVTransport", "Record", {"InstanceID": 0}
    )
    assert response.status == 500
    body = await response.text()
    assert "<errorCode>401</errorCode>" in body
    assert "UPnPError" in body


async def test_malformed_soap_is_rejected_cleanly(bridged):
    client, renderer = bridged
    response = await client.post(
        f"/dev/{renderer.uuid}/svc/AVTransport/control", data=b"<not-soap"
    )
    assert response.status == 500
    assert "errorCode" in await response.text()


# ----------------------------------------------------------------------
# GENA
# ----------------------------------------------------------------------
class CallbackServer:
    """Stands in for the control point's event callback endpoint."""

    def __init__(self) -> None:
        self.notifications: list[dict[str, str]] = []
        self.received = asyncio.Event()

    async def start(self) -> str:
        app = web.Application()
        app.router.add_route("NOTIFY", "/events", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        port = self._runner.addresses[0][1]
        return f"http://127.0.0.1:{port}/events"

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.text()
        self.notifications.append(
            {"seq": request.headers.get("SEQ", ""), "sid": request.headers.get("SID", ""),
             "body": body}
        )
        self.received.set()
        return web.Response(status=200)

    async def stop(self) -> None:
        await self._runner.cleanup()


async def test_subscribe_delivers_an_initial_event_then_updates(bridged):
    client, renderer = bridged
    callback = CallbackServer()
    callback_url = await callback.start()
    try:
        response = await client.request(
            "SUBSCRIBE",
            f"/dev/{renderer.uuid}/svc/AVTransport/event",
            headers={
                "CALLBACK": f"<{callback_url}>",
                "NT": "upnp:event",
                "TIMEOUT": "Second-1800",
            },
        )
        assert response.status == 200
        sid = response.headers["SID"]
        assert sid.startswith("uuid:")
        assert response.headers["TIMEOUT"] == "Second-1800"

        await asyncio.wait_for(callback.received.wait(), 5)
        initial = callback.notifications[0]
        assert initial["seq"] == "0"
        assert initial["sid"] == sid
        values = lastchange.parse(lastchange.parse_propertyset(initial["body"])["LastChange"])
        assert values["TransportState"] == "STOPPED"

        # A state change on the player must reach the same callback.
        callback.received.clear()
        await renderer.on_sonos_avtransport({"TransportState": "PLAYING"})
        await asyncio.wait_for(callback.received.wait(), 5)
        latest = callback.notifications[-1]
        assert latest["seq"] == "1"
        assert 'val="PLAYING"' in latest["body"]

        renewed = await client.request(
            "SUBSCRIBE",
            f"/dev/{renderer.uuid}/svc/AVTransport/event",
            headers={"SID": sid, "TIMEOUT": "Second-600"},
        )
        assert renewed.status == 200
        assert renewed.headers["SID"] == sid

        gone = await client.request(
            "UNSUBSCRIBE",
            f"/dev/{renderer.uuid}/svc/AVTransport/event",
            headers={"SID": sid},
        )
        assert gone.status == 200
        assert len(renderer.subscriptions) == 0
    finally:
        await callback.stop()


async def test_bad_subscribe_requests_are_refused(bridged):
    client, renderer = bridged
    path = f"/dev/{renderer.uuid}/svc/AVTransport/event"
    assert (await client.request("SUBSCRIBE", path, headers={"NT": "upnp:event"})).status == 412
    assert (
        await client.request("SUBSCRIBE", path, headers={"SID": "uuid:nope"})
    ).status == 412
    assert (
        await client.request("UNSUBSCRIBE", path, headers={"SID": "uuid:nope"})
    ).status == 412


async def test_sonos_events_are_translated_and_forwarded(bridged):
    client, renderer = bridged
    callback = CallbackServer()
    callback_url = await callback.start()
    try:
        await client.request(
            "SUBSCRIBE",
            f"/dev/{renderer.uuid}/svc/RenderingControl/event",
            headers={"CALLBACK": f"<{callback_url}>", "NT": "upnp:event"},
        )
        await asyncio.wait_for(callback.received.wait(), 5)
        callback.received.clear()

        sonos_event = lastchange.propertyset(
            {
                "LastChange": (
                    '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/RCS/">'
                    '<InstanceID val="0"><Volume channel="Master" val="66"/>'
                    '<Volume channel="LF" val="100"/>'
                    '<Mute channel="Master" val="1"/></InstanceID></Event>'
                )
            }
        )
        response = await client.request(
            "NOTIFY",
            f"/sonos/{renderer.uuid}/RenderingControl",
            data=sonos_event.encode("utf-8"),
            headers={"NT": "upnp:event", "NTS": "upnp:propchange", "SEQ": "4"},
        )
        assert response.status == 200
        assert renderer.state.volume == 66
        assert renderer.state.mute is True

        await asyncio.wait_for(callback.received.wait(), 5)
        assert 'val="66"' in callback.notifications[-1]["body"]
    finally:
        await callback.stop()


# ----------------------------------------------------------------------
# Status endpoints
# ----------------------------------------------------------------------
async def test_status_endpoints(bridged):
    client, renderer = bridged
    page = await client.get("/")
    assert page.status == 200
    assert "Kitchen (Sonos)" in await page.text()

    data = await (await client.get("/status.json")).json()
    assert data["devices"][0]["room"] == "Kitchen"
    assert data["devices"][0]["udn"] == renderer.udn
    assert data["devices"][0]["iconKind"] == "one"
    assert data["devices"][0]["stereoPair"] is False


async def test_the_status_page_shows_each_room_as_its_own_speaker(bridged):
    client, renderer = bridged
    renderer.zone.model = "Sonos Five"
    renderer.zone.channel_map = "RINCON_AAA01400:LF,LF;RINCON_BBB01400:RF,RF"
    body = await (await client.get("/")).text()
    assert "<svg" in body and "Sonos Five" in body
    assert "stereo pair" in body


async def test_event_callbacks_with_a_foreign_sid_are_rejected(bridged):
    """Once the bridge holds real subscriptions, GENA's SID is what identifies
    the player - anything else must not be able to inject transport state."""
    client, renderer = bridged
    renderer.sonos_sids = {"uuid:the-real-one"}
    body = lastchange.propertyset(
        {
            "LastChange": (
                '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
                '<InstanceID val="0"><TransportState val="PLAYING"/>'
                "</InstanceID></Event>"
            )
        }
    ).encode("utf-8")

    response = await client.request(
        "NOTIFY", f"/sonos/{renderer.uuid}/AVTransport",
        data=body, headers={"SID": "uuid:someone-else"},
    )
    assert response.status == 412
    assert renderer.state.transport_state == "STOPPED"

    response = await client.request(
        "NOTIFY", f"/sonos/{renderer.uuid}/AVTransport",
        data=body, headers={"SID": "uuid:the-real-one"},
    )
    assert response.status == 200
    assert renderer.state.transport_state == "PLAYING"
