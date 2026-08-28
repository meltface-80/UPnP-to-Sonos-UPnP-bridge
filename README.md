<div align="center"> 

<img width="1536" height="1024" alt="image" src="https://github.com/user-attachments/assets/fc1dd26e-db7f-4e27-8f66-b0ab74db89e3" />

</div>

# UPnP to Sonos UPnP bridge for Audirvana - v1.0.2

**📖 Install guide & docs: [meltface-80.github.io/UPnP-to-Sonos-UPnP-bridge-for-Audirvana-](https://meltface-80.github.io/UPnP-to-Sonos-UPnP-bridge-for-Audirvana-/)**

Audirvana can stream to any standard UPnP/DLNA renderer, but not to Sonos. Sonos players
advertise themselves as `ZonePlayer` devices rather than `MediaRenderer` devices, hide their
renderer as an embedded child device, use non-standard control URLs, and reject the metadata a
generic control point sends them. Audirvana never sees them.

This bridge fixes that. It discovers your Sonos players, and publishes each room on the network
as a clean, standards-compliant `MediaRenderer:1` device carrying the room's own name — so
**Kitchen**, **Study** and the rest simply appear in Audirvana's device list and play.

---

## Install

Run it on an always-on Linux machine on the same network as your speakers — a NAS, a Raspberry
Pi, a home server.

If Docker is not installed yet:

```bash
dietpi-software install 162              # DietPi (162 is its Docker package)
curl -fsSL https://get.docker.com | sh   # Debian, Ubuntu, Raspberry Pi OS
```

Then start the bridge:

```bash
docker run -d \
  --name sonos-upnp-bridge \
  --network host \
  --restart unless-stopped \
  ghcr.io/meltface-80/sonos-upnp-bridge:latest
```

That is the whole installation. Open `http://<host-ip>:1500/` to see the rooms it is publishing,
then open Audirvana — your Sonos rooms are in the device list.

> **`--network host` is required.** UPnP discovery is multicast, and multicast does not cross
> Docker's default bridge network. Host networking is what lets the bridge find your players and
> lets Audirvana find the bridge. Docker Desktop for macOS and Windows does not provide real host
> networking, so the container needs a Linux host.

## How it works

```
Audirvana  ──UPnP──▶  bridge  ──Sonos UPnP──▶  Sonos players
     │                                              ▲
     └──────────── audio streamed directly ─────────┘
```

The bridge finds one player over SSDP, then reads the whole household from Sonos'
`ZoneGroupTopology` service: room names, addresses, group membership, and which players are
bonded satellites rather than rooms. Each visible room gets a virtual renderer with a stable
UUID derived from the player's serial, so Audirvana remembers your devices across restarts.

Translating between the two dialects is the interesting part:

- **Metadata is rebuilt.** Sonos rejects third-party items whose DIDL-Lite lacks its
  `RINCON_AssociatedZPUDN` descriptor, and dislikes several protocolInfo forms that are perfectly
  legal UPnP. Incoming metadata is parsed and re-emitted in the shape Sonos accepts.
- **Playback goes through the Sonos queue.** Loading a track and its successor into the queue is
  what makes gapless playback work; Sonos moves between them itself. Audirvana's follow-up calls
  after a track change are recognised and do not restart the transport.
- **Transport follows the group coordinator.** Playing to a grouped room plays to the group,
  which is how Sonos behaves. Volume and mute stay with the individual speaker.
- **State flows back.** The bridge subscribes to each player's events and re-publishes them as
  standard `LastChange` events, so Audirvana's transport display stays in sync — with a periodic
  reconcile as a safety net.

Audio never passes through the bridge. Sonos fetches it straight from Audirvana's own HTTP
server, so there is no extra hop and no transcoding.

## Device icons

Control points show an icon beside each device, so every room gets a line drawing of the speaker
it actually is, seen in three-quarter view: an Era 300 keeps its cinched waist, a Sub the port cut
through it, a soundbar its length, a Move its charging base. **A room that is a bonded stereo pair
is drawn as two speakers**, which makes a paired room obvious at a glance in a device list.

| | |
| --- | --- |
| Five, Play:5, Play:3 | One, One SL, Play:1 |
| Era 100, Era 300 | Beam, Arc, Arc Ultra, Ray |
| Playbar, Playbase | Move, Move 2, Roam, Roam SL |
| Sub, Sub Mini | Amp, Connect:Amp, Port, Connect |
| Symfonisk bookshelf, lamp, picture frame | anything else: a generic speaker |

The model is read from the player's own description document, so nothing needs configuring.
Each cabinet is described once by its real width, depth and height and flattened through one
shared projection at start-up - there are no image files in the repository - then served as PNG
at 48, 120, 240 and 512 pixels, and as SVG at `/dev/<uuid>/icon.svg` for control points and
dashboards that would rather scale it. The bridge's own status page shows the same icons next to
your rooms.

Each icon's URL carries a fingerprint of the drawing behind it
(`/dev/<uuid>/icon/<fingerprint>/120.png`). Control points cache icons hard, so a URL that stayed
the same while the picture changed would leave them showing an old icon indefinitely; a new
drawing is now a new URL. The description also carries a `configId`, UPnP's signal that a cached
description is stale, so a control point knows to read it again after an upgrade.

## Configuration

Everything is optional; pass any of these with `-e NAME=value`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `NAME_SUFFIX` | `" (Sonos)"` | Appended to each room name in Audirvana. Set to `""` for bare room names. |
| `HTTP_PORT` | `1500` | Port for device descriptions, control endpoints and the status page. |
| `SONOS_HOSTS` | — | Comma-separated player IPs, for when multicast discovery is unreliable. One is enough — the rest are read from the topology. |
| `INCLUDE_ZONES` | — | Publish only these rooms, e.g. `Kitchen,Study`. |
| `EXCLUDE_ZONES` | — | Publish everything except these rooms. |
| `BRIDGE_MODE` | `queue` | `queue` for gapless playback via the Sonos queue; `direct` loads each track straight onto the transport. |
| `UNGROUP_ON_PLAY` | `false` | Detach a room from its Sonos group before playing to it. |
| `BRIDGE_IP` | auto | Address to advertise, for hosts with several interfaces. |
| `SSDP_PORT` | `1900` | Change only if something else on the host owns UDP 1900. |
| `DISCOVERY_INTERVAL` | `60` | Seconds between SSDP searches for new players. |
| `TOPOLOGY_INTERVAL` | `30` | Seconds between topology refreshes (new rooms, renames, regrouping). |
| `POLL_INTERVAL` | `10` | Seconds between reconciliation polls for rooms a control point is watching. |
| `EXTRA_PROTOCOL_INFO` | — | Extra `protocolInfo` entries to advertise, comma-separated. |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every UPnP action in both directions. |

Example:

```bash
docker run -d --name sonos-upnp-bridge --network host --restart unless-stopped \
  -e NAME_SUFFIX="" \
  -e EXCLUDE_ZONES="Bathroom,Garage" \
  ghcr.io/meltface-80/sonos-upnp-bridge:latest
```

## Formats

Sonos accepts FLAC, ALAC, WAV, AIFF, MP3, AAC and Ogg over HTTP, up to 24-bit/48 kHz on current
S2 hardware — and does not play DSD at all. `protocolInfo` cannot express a sample-rate ceiling,
so set Audirvana's maximum sample rate for the device to match your speakers; the bridge
advertises formats but cannot resample.

## Troubleshooting

Start at `http://<host-ip>:1500/`, which shows exactly what the bridge can see.

**The table is empty.** The bridge has not found any players. Check that the container really is
on host networking, that the host shares a subnet with the speakers, and that nothing else on the
host holds UDP 1900 (`ss -lunp | grep 1900`). Setting `SONOS_HOSTS` to one player's IP skips
discovery entirely.

**Rooms are listed but Audirvana does not show them.** That is the second half of the same
multicast path. Audirvana must be on the same subnet, and some routers and access points filter
multicast between wired and wireless clients — look for IGMP snooping or "multicast enhancement"
settings. Restarting Audirvana forces a fresh search.

**Playback starts then stops.** Usually a format Sonos will not take. Run with `LOG_LEVEL=DEBUG`
and look for a UPnP error 714 (illegal MIME type) or 701, then lower the sample rate or change
format in Audirvana.

**Playing to one room plays everywhere.** The room is grouped in the Sonos app, and transport
commands belong to the group coordinator. Ungroup it, or set `UNGROUP_ON_PLAY=true`.

## Building and developing

```bash
# Build the image yourself
docker build -t sonos-upnp-bridge .
docker run -d --name sonos-upnp-bridge --network host sonos-upnp-bridge

# Run it straight from a checkout
pip install -r requirements.txt
python -m sonosbridge

# Tests: unit coverage plus an end-to-end run of the real bridge
# against a simulated Sonos player
pip install -r requirements-dev.txt
python -m pytest
```

Layout: `sonosbridge/renderer.py` holds the translation logic, `sonosbridge/sonos.py` the Sonos
dialect, `sonosbridge/ssdp.py` discovery and advertisement, `sonosbridge/server.py` the HTTP,
SOAP and GENA surface, and `sonosbridge/bridge.py` wires it together.

## Project page

The site under `docs/` is published with GitHub Pages: **Settings → Pages → Source: Deploy from a
branch → `main` / `/docs`**.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Sonos, Inc. or Audirvana.
