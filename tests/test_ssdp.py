from sonosbridge import ssdp
from sonosbridge.config import Config
from sonosbridge.net import parse_headers
from sonosbridge.renderer import VirtualRenderer
from sonosbridge.sonos import ZoneInfo

from .conftest import StubTopology


def make_renderer():
    zone = ZoneInfo(uid="RINCON_AAA01400", name="Kitchen", ip="192.168.1.10",
                    coordinator_uid="RINCON_AAA01400")
    return VirtualRenderer(
        Config(), zone, StubTopology({zone.uid: zone}), None, "http://192.168.1.2:1500"
    )


def test_a_renderer_announces_every_required_target():
    renderer = make_renderer()
    targets = dict(ssdp.notification_targets(renderer))
    assert "upnp:rootdevice" in targets
    assert renderer.udn in targets
    assert "urn:schemas-upnp-org:device:MediaRenderer:1" in targets
    assert "urn:schemas-upnp-org:service:AVTransport:1" in targets
    assert "urn:schemas-upnp-org:service:RenderingControl:1" in targets
    assert "urn:schemas-upnp-org:service:ConnectionManager:1" in targets

    assert targets["upnp:rootdevice"] == f"{renderer.udn}::upnp:rootdevice"
    assert targets[renderer.udn] == renderer.udn  # the uuid target has a bare USN


def test_search_target_matching():
    assert ssdp.matches("ssdp:all", "upnp:rootdevice")
    assert ssdp.matches("upnp:rootdevice", "upnp:rootdevice")
    assert not ssdp.matches("upnp:rootdevice", "urn:schemas-upnp-org:device:MediaRenderer:1")
    assert ssdp.matches(
        "urn:schemas-upnp-org:device:MediaRenderer:1",
        "urn:schemas-upnp-org:device:MediaRenderer:1",
    )


def test_a_media_renderer_search_finds_the_bridge():
    renderer = make_renderer()
    hits = [
        (nt, usn)
        for nt, usn in ssdp.notification_targets(renderer)
        if ssdp.matches("urn:schemas-upnp-org:device:MediaRenderer:1", nt)
    ]
    assert hits == [
        (
            "urn:schemas-upnp-org:device:MediaRenderer:1",
            f"{renderer.udn}::urn:schemas-upnp-org:device:MediaRenderer:1",
        )
    ]


def test_msearch_headers_are_parsed():
    request = (
        b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
        b'MAN: "ssdp:discover"\r\nMX: 3\r\n'
        b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n"
    )
    headers = parse_headers(request)
    assert headers[""].startswith("M-SEARCH")
    assert headers["man"].strip('"') == "ssdp:discover"
    assert headers["mx"] == "3"


def test_sonos_announcements_are_recognised():
    notify = (
        b"NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
        b"NT: urn:schemas-upnp-org:device:ZonePlayer:1\r\nNTS: ssdp:alive\r\n"
        b"USN: uuid:RINCON_AAA01400::urn:schemas-upnp-org:device:ZonePlayer:1\r\n"
        b"LOCATION: http://192.168.1.10:1400/xml/device_description.xml\r\n\r\n"
    )
    seen = []
    server = ssdp.SsdpServer("192.168.1.2", lambda: [], on_sonos_seen=seen.append)
    server.handle_datagram(notify, ("192.168.1.10", 1900))
    assert seen == ["192.168.1.10"]


def test_a_departing_sonos_is_not_treated_as_discovery():
    byebye = (
        b"NOTIFY * HTTP/1.1\r\nNT: urn:schemas-upnp-org:device:ZonePlayer:1\r\n"
        b"NTS: ssdp:byebye\r\nUSN: uuid:RINCON_AAA01400\r\n\r\n"
    )
    seen = []
    server = ssdp.SsdpServer("192.168.1.2", lambda: [], on_sonos_seen=seen.append)
    server.handle_datagram(byebye, ("192.168.1.10", 1900))
    assert seen == []
