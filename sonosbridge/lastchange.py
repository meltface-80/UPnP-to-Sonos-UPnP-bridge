"""LastChange event documents (AVTransport / RenderingControl)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from xml.sax.saxutils import escape, quoteattr

from defusedxml import ElementTree as DET

LOGGER = logging.getLogger(__name__)

AVT_EVENT_NS = "urn:schemas-upnp-org:metadata-1-0/AVT/"
RCS_EVENT_NS = "urn:schemas-upnp-org:metadata-1-0/RCS/"
RINCON_NS = "urn:schemas-rinconnetworks-com:metadata-1-0/"

# RenderingControl variables that carry a channel attribute.
CHANNEL_VARIABLES = frozenset({"Volume", "Mute", "Loudness", "VolumeDB"})


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse(xml_text: str, instance_id: str = "0") -> dict[str, str]:
    """Flatten a LastChange document into ``{variable: value}``.

    Channel-qualified variables are keyed ``"Volume/Master"``.  Sonos-private
    variables (the ``r:`` namespace) keep their bare local name, so
    ``r:NextTrackURI`` becomes ``NextTrackURI``.
    """
    values: dict[str, str] = {}
    text = (xml_text or "").strip()
    if not text:
        return values
    try:
        root = DET.fromstring(text)
    except Exception as exc:
        LOGGER.debug("Could not parse LastChange: %s", exc)
        return values

    for instance in root:
        if _localname(instance.tag) != "InstanceID":
            continue
        if instance.get("val", "0") != instance_id:
            continue
        for node in instance:
            name = _localname(node.tag)
            value = node.get("val")
            if value is None:
                continue
            channel = node.get("channel")
            key = f"{name}/{channel}" if channel else name
            values[key] = value
    return values


def build(
    namespace: str,
    variables: Mapping[str, str],
    instance_id: str = "0",
) -> str:
    """Render a LastChange document from a flat ``{variable: value}`` mapping."""
    parts = [f"<Event xmlns={quoteattr(namespace)}>", f'<InstanceID val="{instance_id}">']
    for key, value in variables.items():
        name, _, channel = key.partition("/")
        attrs = ""
        if channel:
            attrs = f" channel={quoteattr(channel)}"
        elif name in CHANNEL_VARIABLES:
            attrs = ' channel="Master"'
        parts.append(f"<{name}{attrs} val={quoteattr('' if value is None else str(value))}/>")
    parts.append("</InstanceID></Event>")
    return "".join(parts)


def build_avtransport(variables: Mapping[str, str]) -> str:
    return build(AVT_EVENT_NS, variables)


def build_rendering_control(variables: Mapping[str, str]) -> str:
    return build(RCS_EVENT_NS, variables)


def propertyset(properties: Mapping[str, str]) -> str:
    """Wrap event properties in the GENA ``propertyset`` envelope."""
    parts = ['<?xml version="1.0" encoding="utf-8"?>',
             '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">']
    for name, value in properties.items():
        parts.append(
            f"<e:property><{name}>{escape('' if value is None else str(value))}</{name}></e:property>"
        )
    parts.append("</e:propertyset>")
    return "".join(parts)


def parse_propertyset(xml_text: str) -> dict[str, str]:
    """Extract ``{property: value}`` from a GENA ``propertyset`` document."""
    values: dict[str, str] = {}
    text = (xml_text or "").strip()
    if not text:
        return values
    try:
        root = DET.fromstring(text)
    except Exception as exc:
        LOGGER.debug("Could not parse propertyset: %s", exc)
        return values
    for prop in root:
        if _localname(prop.tag) != "property":
            continue
        for node in prop:
            values[_localname(node.tag)] = node.text or ""
    return values
