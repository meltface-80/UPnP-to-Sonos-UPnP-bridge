#!/usr/bin/env python3
"""Does Plexamp offer to play to a device we invent?  A throwaway experiment.

Plexamp cannot cast to a UPnP renderer, so the bridge is invisible to it.  It
*can* cast to a Plex player, which is a much smaller thing than it sounds: a
device that announces itself and answers a handful of HTTP requests.  This
script pretends to be one and does nothing else - it accepts a play command,
logs it, and plays nothing.  The question it answers is only "does the device
show up in the cast list, and what does Plexamp ask it for", which decides
whether teaching the bridge to be one is worth doing.

There are two ways a player is found, and they are not equivalent:

  GDM      A Plex-flavoured multicast search on the local network.  Desktop
           Plexamp and Plex Web use it.  Nothing leaves the LAN.  On by
           default here.

  plex.tv  The player is registered to your Plex account, which then hands
           out the local address to reach it on.  Plexamp on iOS and Android
           uses only this - it sends no GDM traffic at all - so on a phone
           this is the only way.  Run with --link to try it.

The distinction matters for the reason you are reading this: --link puts the
player's *identity* in your Plex account, once.  Playback and control still go
straight to this machine over the LAN afterwards.  Nothing is streamed through
plex.tv either way.

    python3 tools/plex_player_spike.py                 # LAN only
    python3 tools/plex_player_spike.py --link          # also register on plex.tv

Then open Plexamp and look at its list of things to play to.  Everything the
player is asked for is logged here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from xml.sax.saxutils import quoteattr

LOGGER = logging.getLogger("plexspike")

# GDM: the same idea as SSDP, on Plex's own multicast group.  Players listen
# for searches on 32412; 32414 is the servers' equivalent.
GDM_ADDR = "239.0.0.250"
GDM_PLAYER_PORT = 32412
SEARCH = b"M-SEARCH * HTTP/1.0"

# What a player claims it can do.  A controller reads this before offering the
# device, so "playback" and "timeline" are the two that earn us a place in the
# cast list.
CAPABILITIES = "timeline,playback,playqueues,provider-playback"
PRODUCT = "Sonos UPnP Bridge"
VERSION = "0.0-spike"
PLATFORM = "Linux"
DEVICE_CLASS = "stb"

PLEX_TV = "https://plex.tv"
DEFAULT_PORT = 32500


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------
class Identity:
    """Who this player says it is, kept across runs.

    The client identifier has to survive a restart: it is what a controller
    remembers a player by, and a new one every time would look like a new
    device every time.
    """

    def __init__(self, path: pathlib.Path, name: str, port: int) -> None:
        self.path = path
        self.name = name
        self.port = port
        self.token: str | None = None
        self.client_id = str(uuid.uuid4())
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.client_id = saved.get("client_id", self.client_id)
            self.token = saved.get("token")
        else:
            self.save()

    def save(self) -> None:
        self.path.write_text(
            json.dumps({"client_id": self.client_id, "token": self.token}, indent=2),
            encoding="utf-8",
        )

    def headers(self) -> dict[str, str]:
        return {
            "X-Plex-Client-Identifier": self.client_id,
            "X-Plex-Product": PRODUCT,
            "X-Plex-Version": VERSION,
            "X-Plex-Device": PRODUCT,
            "X-Plex-Device-Name": self.name,
            "X-Plex-Platform": PLATFORM,
            "X-Plex-Model": "spike",
            # The reason a controller will offer this device as a target.
            "X-Plex-Provides": "client,player,pubsub-player",
            "Accept": "application/json",
        }


# ----------------------------------------------------------------------
# GDM
# ----------------------------------------------------------------------
class GdmResponder(asyncio.DatagramProtocol):
    """Answers the multicast search desktop Plex clients send."""

    def __init__(self, identity: Identity) -> None:
        self.identity = identity
        self.transport: asyncio.DatagramTransport | None = None
        self.seen: set[str] = set()

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if not data.startswith(SEARCH):
            return
        host = addr[0]
        if host not in self.seen:
            self.seen.add(host)
            LOGGER.info("GDM  search from %s - answering", host)
        else:
            LOGGER.debug("GDM  search from %s", host)
        if self.transport is not None:
            self.transport.sendto(self.hello(), addr)

    def hello(self) -> bytes:
        fields = {
            "Content-Type": "plex/media-player",
            "Resource-Identifier": self.identity.client_id,
            "Name": self.identity.name,
            "Port": str(self.identity.port),
            "Product": PRODUCT,
            "Version": VERSION,
            "Protocol": "plex",
            "Protocol-Version": "1",
            "Protocol-Capabilities": CAPABILITIES,
            "Device-Class": DEVICE_CLASS,
            "Updated-At": str(int(time.time())),
        }
        head = "HTTP/1.0 200 OK\r\n"
        body = "".join(f"{key}: {value}\r\n" for key, value in fields.items())
        return (head + body + "\r\n").encode("ascii")


async def start_gdm(identity: Identity, bind_ip: str) -> asyncio.DatagramTransport | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    try:
        sock.bind(("", GDM_PLAYER_PORT))
        membership = struct.pack("4s4s", socket.inet_aton(GDM_ADDR), socket.inet_aton(bind_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    except OSError as exc:
        sock.close()
        LOGGER.error("GDM  could not listen on %s:%d (%s)", GDM_ADDR, GDM_PLAYER_PORT, exc)
        LOGGER.error("     multicast needs host networking - not a Docker bridge network")
        return None

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: GdmResponder(identity), sock=sock)
    LOGGER.info("GDM  listening on %s:%d as %r", GDM_ADDR, GDM_PLAYER_PORT, identity.name)
    return transport


# ----------------------------------------------------------------------
# The player's HTTP side
# ----------------------------------------------------------------------
def resources_xml(identity: Identity) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<MediaContainer size="1">'
        f"<Player title={quoteattr(identity.name)}"
        f" machineIdentifier={quoteattr(identity.client_id)}"
        f" product={quoteattr(PRODUCT)} platform={quoteattr(PLATFORM)}"
        f" platformVersion={quoteattr(VERSION)} version={quoteattr(VERSION)}"
        f" protocol=\"plex\" protocolVersion=\"1\""
        f" protocolCapabilities={quoteattr(CAPABILITIES)}"
        f" deviceClass={quoteattr(DEVICE_CLASS)}/>"
        "</MediaContainer>"
    )


def timeline_xml(command_id: str) -> str:
    """A stopped timeline for each media type, which is what an idle player reports."""
    timelines = "".join(
        f'<Timeline type="{kind}" state="stopped" controllable="{CAPABILITIES}"/>'
        for kind in ("music", "video", "photo")
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<MediaContainer commandID="{command_id}" location="navigation">'
        f"{timelines}</MediaContainer>"
    )


class PlayerHttp:
    """Just enough HTTP to be interrogated, and a log of everything asked."""

    def __init__(self, identity: Identity) -> None:
        self.identity = identity
        self.requests = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not line:
                return
            method, _, rest = line.decode("latin-1").strip().partition(" ")
            target = rest.rsplit(" ", 1)[0]

            headers: dict[str, str] = {}
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=10)
                if raw in (b"\r\n", b"\n", b""):
                    break
                key, _, value = raw.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()

            body, status, kind = self.route(method, target, headers)
            await self.respond(writer, status, kind, body)
        except (TimeoutError, ConnectionResetError):
            pass
        finally:
            writer.close()

    def route(self, method: str, target: str, headers: dict[str, str]):
        path, _, query = target.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        self.requests += 1

        who = headers.get("x-plex-device-name") or headers.get("x-plex-product") or "?"
        interesting = {k: v for k, v in params.items() if k != "X-Plex-Token"}
        LOGGER.info("HTTP %s %s  from %s", method, path, who)
        if interesting:
            LOGGER.info("     %s", interesting)

        if path == "/resources":
            return resources_xml(self.identity), 200, "text/xml"
        if path.startswith("/player/timeline"):
            return timeline_xml(params.get("commandID", "0")), 200, "text/xml"
        if path.startswith("/player/"):
            # A real player would act on this.  Here, noticing it is the point.
            LOGGER.info("     *** this is a command - the cast target works ***")
            return "", 200, "text/plain"
        return "", 404, "text/plain"

    async def respond(self, writer: asyncio.StreamWriter, status: int, kind: str, body: str) -> None:
        payload = body.encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Not Found'}\r\n"
            f"Content-Type: {kind}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"X-Plex-Client-Identifier: {self.identity.client_id}\r\n"
            "X-Plex-Protocol: 1.0\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Expose-Headers: X-Plex-Client-Identifier\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1") + payload)
        await writer.drain()


# ----------------------------------------------------------------------
# Registering with plex.tv, for the clients that will not look on the LAN
# ----------------------------------------------------------------------
def plex_request(method: str, url: str, identity: Identity, token: str | None = None):
    headers = identity.headers()
    if token:
        headers["X-Plex-Token"] = token
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, str(exc)


def link(identity: Identity, address: str) -> bool:
    """Claim a PIN, wait for it to be entered, then publish where we live."""
    status, pin = plex_request("POST", f"{PLEX_TV}/api/v2/pins?strong=true", identity)
    if status not in (200, 201) or not isinstance(pin, dict):
        LOGGER.error("link  could not start the PIN flow: %s %s", status, pin)
        return False

    print()
    print("  ┌─────────────────────────────────────────────┐")
    print("  │  Open https://plex.tv/link and enter         │")
    print(f"  │  {pin['code']:<42} │")
    print("  └─────────────────────────────────────────────┘")
    print()

    deadline = time.time() + 300
    token = None
    while time.time() < deadline:
        time.sleep(3)
        status, body = plex_request("GET", f"{PLEX_TV}/api/v2/pins/{pin['id']}", identity)
        if isinstance(body, dict) and body.get("authToken"):
            token = body["authToken"]
            break
    if not token:
        LOGGER.error("link  nobody entered the code")
        return False

    identity.token = token
    identity.save()
    LOGGER.info("link  registered, token saved to %s", identity.path)

    # plex.tv now knows the player exists; it also has to know how to reach it.
    status, devices = plex_request("GET", f"{PLEX_TV}/api/v2/resources", identity, token)
    device_id = None
    if isinstance(devices, list):
        for device in devices:
            if device.get("clientIdentifier") == identity.client_id:
                device_id = device.get("id")
                break
    if device_id is None:
        LOGGER.warning("link  could not find this device in the account's list")
        LOGGER.warning("      plex.tv said: %s %s", status, str(devices)[:400])
        return True

    uri = f"http://{address}:{identity.port}"
    published = urllib.parse.quote(uri, safe="")
    status, body = plex_request(
        "PUT", f"{PLEX_TV}/devices/{device_id}?Connection[][uri]={published}", identity, token
    )
    LOGGER.info("link  published %s -> %s %s", uri, status, str(body)[:200])
    return True


# ----------------------------------------------------------------------
def local_address() -> str:
    """The address on the interface that reaches the LAN."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


async def run(args) -> int:
    address = args.address or local_address()
    state = pathlib.Path(args.state).expanduser()
    identity = Identity(state, args.name, args.port)

    if args.link and not link(identity, address):
        return 1

    http = PlayerHttp(identity)
    try:
        server = await asyncio.start_server(http.handle, "0.0.0.0", args.port)
    except OSError as exc:
        LOGGER.error("HTTP could not listen on port %d (%s)", args.port, exc)
        return 1
    LOGGER.info("HTTP listening on http://%s:%d", address, args.port)

    gdm = None
    if not args.no_gdm:
        gdm = await start_gdm(identity, address)

    LOGGER.info("id   %s", identity.client_id)
    LOGGER.info("")
    LOGGER.info("Now open Plexamp and look for %r in its list of players.", args.name)
    LOGGER.info("  desktop Plexamp / Plex Web  should find it over GDM")
    LOGGER.info("  Plexamp on a phone          only sees it if you ran --link")
    LOGGER.info("Every request it makes is logged below.  Ctrl-C to stop.")
    LOGGER.info("")

    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        if gdm is not None:
            gdm.close()
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", default="Sonos Bridge (spike)", help="name shown in Plexamp")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="player HTTP port")
    parser.add_argument("--address", default="", help="LAN address to advertise (default: detect)")
    parser.add_argument("--link", action="store_true", help="also register on plex.tv")
    parser.add_argument("--no-gdm", action="store_true", help="skip LAN discovery")
    parser.add_argument(
        "--state",
        default="~/.plex-player-spike.json",
        help="where the client identifier and token are kept",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
