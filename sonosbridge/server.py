"""The bridge's HTTP surface.

Serves the virtual device descriptions and SCPDs, accepts SOAP control requests
and GENA subscriptions from Audirvana, receives event callbacks from the real
Sonos players, and renders a small status page for troubleshooting.
"""

from __future__ import annotations

import email.utils
import html
import logging
from pathlib import Path

from aiohttp import web

from . import lastchange
from .config import BRIDGE_NAME, BRIDGE_VERSION
from .gena import parse_callbacks, parse_timeout
from .icon import ICON_SIZES
from .icon import render as render_icon
from .renderer import SERVICE_TYPES
from .soap import UPnPError, build_fault, build_response, parse_action
from .speakers import label as speaker_label
from .speakers import svg as speaker_svg
from .ssdp import server_header

LOGGER = logging.getLogger(__name__)

SCPD_DIR = Path(__file__).parent / "scpd"
BRIDGE_KEY: web.AppKey = web.AppKey("bridge")
XML_CONTENT_TYPE = 'text/xml; charset="utf-8"'


def _xml_response(body: str, status: int = 200) -> web.Response:
    return web.Response(
        body=body.encode("utf-8"),
        status=status,
        headers={
            "Content-Type": XML_CONTENT_TYPE,
            "SERVER": server_header(),
            "DATE": email.utils.formatdate(usegmt=True),
            "EXT": "",
        },
    )


def create_app(bridge) -> web.Application:
    app = web.Application()
    app[BRIDGE_KEY] = bridge

    app.router.add_get("/", handle_status_page)
    app.router.add_get("/status.json", handle_status_json)
    app.router.add_get("/scpd/{name}.xml", handle_scpd)
    app.router.add_get("/dev/{uuid}/desc.xml", handle_description)
    app.router.add_get("/dev/{uuid}/icon/{token}/{size}.png", handle_icon)
    app.router.add_get("/dev/{uuid}/icon/{size}.png", handle_icon)
    app.router.add_get("/dev/{uuid}/icon.svg", handle_icon_svg)
    app.router.add_post("/dev/{uuid}/svc/{service}/control", handle_control)
    app.router.add_route("SUBSCRIBE", "/dev/{uuid}/svc/{service}/event", handle_subscribe)
    app.router.add_route("UNSUBSCRIBE", "/dev/{uuid}/svc/{service}/event", handle_unsubscribe)
    app.router.add_route("NOTIFY", "/sonos/{uuid}/{service}", handle_sonos_event)
    return app


def _renderer(request: web.Request):
    bridge = request.app[BRIDGE_KEY]
    renderer = bridge.renderer_for(request.match_info["uuid"])
    if renderer is None:
        raise web.HTTPNotFound(text="Unknown device")
    return renderer


def _service_name(request: web.Request) -> str:
    name = request.match_info["service"]
    if name not in SERVICE_TYPES:
        raise web.HTTPNotFound(text="Unknown service")
    return name


# ----------------------------------------------------------------------
# Descriptions
# ----------------------------------------------------------------------
async def handle_description(request: web.Request) -> web.Response:
    return _xml_response(_renderer(request).description_xml())


async def handle_scpd(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if name not in SERVICE_TYPES:
        raise web.HTTPNotFound(text="Unknown service")
    path = SCPD_DIR / f"{name}.xml"
    return _xml_response(path.read_text(encoding="utf-8"))


async def handle_icon(request: web.Request) -> web.Response:
    """The room's icon.

    The path a description advertises carries a fingerprint of the drawing, so
    a control point that cached one can never be shown a stale one.  The
    fingerprint is not checked here: a client still holding an older
    description asks for the old path and gets the current drawing, rather than
    a 404.  That unfingerprinted path is only cached briefly, so such a client
    recovers on its own.
    """
    renderer = _renderer(request)
    try:
        size = int(request.match_info["size"])
    except ValueError as exc:
        raise web.HTTPNotFound(text="Unknown icon") from exc
    if size not in ICON_SIZES:
        raise web.HTTPNotFound(text="Unknown icon size")
    stamped = "token" in request.match_info
    cache = "max-age=86400, immutable" if stamped else "max-age=300"
    return web.Response(
        body=render_icon(size, renderer.icon_kind, renderer.stereo_pair),
        headers={"Content-Type": "image/png", "Cache-Control": cache},
    )


async def handle_icon_svg(request: web.Request) -> web.Response:
    """The same glyph as vector art, for control points and dashboards that
    would rather scale it than take the 48px PNG."""
    renderer = _renderer(request)
    body = speaker_svg(
        renderer.icon_kind,
        renderer.stereo_pair,
        size=128,
        title=renderer.zone.model or speaker_label(renderer.icon_kind),
        auto_theme=True,
    )
    return web.Response(
        text=body,
        headers={"Content-Type": "image/svg+xml", "Cache-Control": "max-age=86400"},
    )


# ----------------------------------------------------------------------
# SOAP control
# ----------------------------------------------------------------------
async def handle_control(request: web.Request) -> web.Response:
    renderer = _renderer(request)
    service_name = _service_name(request)
    body = await request.text()

    try:
        _, action, args = parse_action(body)
    except UPnPError as exc:
        return _xml_response(build_fault(exc.code, exc.description), status=500)
    except Exception as exc:
        LOGGER.debug("Malformed SOAP request: %s", exc)
        return _xml_response(build_fault(401, "Invalid Action"), status=500)

    LOGGER.debug("%s: %s.%s %s", renderer.zone.name, service_name, action, args)
    try:
        result = await renderer.handle_action(service_name, action, args)
    except UPnPError as exc:
        LOGGER.info(
            "%s: %s.%s failed: %s", renderer.zone.name, service_name, action, exc
        )
        return _xml_response(build_fault(exc.code, exc.description), status=500)
    except TimeoutError:
        LOGGER.warning("%s: %s.%s timed out", renderer.zone.name, service_name, action)
        return _xml_response(build_fault(501, "Sonos player did not respond"), status=500)
    except Exception:
        LOGGER.exception("%s: %s.%s crashed", renderer.zone.name, service_name, action)
        return _xml_response(build_fault(501, "Action Failed"), status=500)

    return _xml_response(build_response(SERVICE_TYPES[service_name], action, result))


# ----------------------------------------------------------------------
# GENA (control point -> bridge)
# ----------------------------------------------------------------------
async def handle_subscribe(request: web.Request) -> web.Response:
    renderer = _renderer(request)
    service_name = _service_name(request)
    manager = renderer.subscriptions
    timeout = parse_timeout(request.headers.get("TIMEOUT", ""), manager.default_timeout)
    sid = request.headers.get("SID", "").strip()

    if sid:
        subscription = await manager.renew(sid, timeout)
        if subscription is None:
            return web.Response(status=412, text="Precondition Failed")
        return _subscribe_response(subscription.sid, subscription.timeout)

    callbacks = parse_callbacks(request.headers.get("CALLBACK", ""))
    if not callbacks or request.headers.get("NT", "") != "upnp:event":
        return web.Response(status=412, text="Precondition Failed")

    subscription = await manager.subscribe(service_name, callbacks, timeout)
    LOGGER.info(
        "%s: %s subscription from %s",
        renderer.zone.name,
        service_name,
        callbacks[0],
    )
    # The initial event must follow the response, never precede it.
    manager.send_initial(subscription, renderer.initial_event(service_name))
    return _subscribe_response(subscription.sid, subscription.timeout)


def _subscribe_response(sid: str, timeout: int) -> web.Response:
    return web.Response(
        status=200,
        headers={
            "SID": sid,
            "TIMEOUT": f"Second-{timeout}",
            "SERVER": server_header(),
            "DATE": email.utils.formatdate(usegmt=True),
            "Content-Length": "0",
        },
    )


async def handle_unsubscribe(request: web.Request) -> web.Response:
    renderer = _renderer(request)
    _service_name(request)
    sid = request.headers.get("SID", "").strip()
    if not sid or request.headers.get("CALLBACK") or request.headers.get("NT"):
        return web.Response(status=412, text="Precondition Failed")
    if not await renderer.subscriptions.unsubscribe(sid):
        return web.Response(status=412, text="Precondition Failed")
    return web.Response(status=200)


# ----------------------------------------------------------------------
# GENA (Sonos -> bridge)
# ----------------------------------------------------------------------
async def handle_sonos_event(request: web.Request) -> web.Response:
    renderer = _renderer(request)
    service_name = _service_name(request)

    # GENA authenticates event callbacks by SID. Once the bridge holds real
    # subscriptions, anything arriving under a different SID is not our player.
    sid = request.headers.get("SID", "").strip()
    if renderer.sonos_sids and sid not in renderer.sonos_sids:
        LOGGER.debug("%s: ignoring event with unknown SID %r", renderer.zone.name, sid)
        return web.Response(status=412, text="Precondition Failed")

    body = await request.text()
    properties = lastchange.parse_propertyset(body)
    raw = properties.get("LastChange", "")
    if raw:
        values = lastchange.parse(raw)
        if values:
            if service_name == "AVTransport":
                await renderer.on_sonos_avtransport(values)
            elif service_name == "RenderingControl":
                await renderer.on_sonos_renderingcontrol(values)
    return web.Response(status=200)


# ----------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------
async def handle_status_json(request: web.Request) -> web.Response:
    return web.json_response(request.app[BRIDGE_KEY].status())


async def handle_status_page(request: web.Request) -> web.Response:
    status = request.app[BRIDGE_KEY].status()
    rows = []
    for device in status["devices"]:
        icon = speaker_svg(
            str(device["iconKind"]),
            bool(device["stereoPair"]),
            size=30,
            title=str(device["model"]) or speaker_label(str(device["iconKind"])),
        )
        model = str(device["model"]) or "-"
        if device["stereoPair"]:
            model += " (stereo pair)"
        rows.append(
            "<tr>"
            f'<td><span class="room"><span class="glyph">{icon}</span>'
            f"<strong>{html.escape(str(device['friendlyName']))}</strong></span></td>"
            f"<td>{html.escape(model)}</td>"
            f"<td>{html.escape(str(device['sonosIp']))}</td>"
            f"<td>{html.escape(str(device['coordinator']))}</td>"
            f"<td>{html.escape(str(device['transportState']))}</td>"
            f"<td>{device['volume']}{' (muted)' if device['mute'] else ''}</td>"
            f"<td>{device['subscribers']}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(
            '<tr><td colspan="7">No Sonos players found yet. Check host '
            "networking, or set <code>SONOS_HOSTS</code>.</td></tr>"
        )

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(BRIDGE_NAME)}</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        margin: 0; padding: 2rem 1.25rem; background: #f6f7f9; color: #16181d; }}
 @media (prefers-color-scheme: dark) {{ body {{ background: #14161a; color: #e8eaee; }} }}
 main {{ max-width: 60rem; margin: 0 auto; }}
 h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
 p.sub {{ margin: 0 0 1.5rem; opacity: .7; }}
 table {{ border-collapse: collapse; width: 100%; background: #fff; border-radius: 10px;
          overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.12); }}
 @media (prefers-color-scheme: dark) {{ table {{ background: #1d2026; }} }}
 th, td {{ text-align: left; padding: .6rem .8rem; border-bottom: 1px solid rgba(128,128,128,.2); }}
 th {{ font-size: .75rem; text-transform: uppercase; letter-spacing: .04em; opacity: .65; }}
 tr:last-child td {{ border-bottom: 0; }}
 code {{ background: rgba(128,128,128,.16); padding: .1rem .35rem; border-radius: 4px; }}
 .room {{ display: flex; align-items: center; gap: .6rem; white-space: nowrap; }}
 .glyph {{ display: inline-flex; opacity: .8; flex: 0 0 auto; }}
 footer {{ margin-top: 1.5rem; font-size: .85rem; opacity: .65; }}
</style></head><body><main>
<h1>{html.escape(BRIDGE_NAME)} <span style="opacity:.5">v{BRIDGE_VERSION}</span></h1>
<p class="sub">Advertising {len(status['devices'])} Sonos room(s) to UPnP control points
from <code>{html.escape(str(status['bridgeIp']))}:{status['httpPort']}</code>
in <code>{html.escape(str(status['mode']))}</code> mode.</p>
<table>
<thead><tr><th>Room</th><th>Model</th><th>Sonos IP</th><th>Group coordinator</th>
<th>Transport</th><th>Volume</th><th>Subscribers</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<footer>Machine-readable status: <a href="/status.json">/status.json</a></footer>
</main></body></html>"""
    return web.Response(text=page, content_type="text/html")
