"""The per-model device icons: matching, geometry and the PNGs served."""

from __future__ import annotations

import struct

import pytest

from sonosbridge.deviceicons import ICON_NAMES, VIEW, icon_for_model, polylines
from sonosbridge.icon import ICON_SIZES, icon_list_xml, render

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == PNG_MAGIC
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("Sonos One", "one"),
        ("Sonos One SL", "one"),
        ("PLAY:1", "one"),
        ("Sonos Era 100", "era100"),
        ("Sonos Era 300", "era300"),
        ("Sonos Five", "five"),
        ("PLAY:5", "five"),
        ("PLAY:3", "play3"),
        ("Sonos Arc", "arc"),
        ("Sonos Arc Ultra", "arc"),
        ("Sonos Beam", "beam"),
        ("Sonos Ray", "ray"),
        ("PLAYBAR", "playbar"),
        ("PLAYBASE", "playbase"),
        ("Sonos Sub", "sub"),
        ("Sonos Sub Mini", "submini"),
        ("Sonos Move", "move"),
        ("Sonos Move 2", "move"),
        ("Sonos Roam SL", "roam"),
        ("Sonos Amp", "amp"),
        ("Sonos CONNECT:AMP", "amp"),
        ("ZP120", "amp"),
        ("Sonos Port", "port"),
        ("Sonos CONNECT", "port"),
        ("ZP80", "port"),
    ],
)
def test_model_names_pick_their_own_drawing(model, expected):
    assert icon_for_model(model) == expected


@pytest.mark.parametrize("model", ["", "   ", "Sonos", "Sonos Something New", "SYMFONISK"])
def test_unknown_models_fall_back(model):
    assert icon_for_model(model) == "generic"


def test_longer_keys_win():
    """"one sl" must not be swallowed by "one", nor "connect:amp" by either half."""
    assert icon_for_model("One SL") != icon_for_model("Sonos Sub Mini")
    assert icon_for_model("Connect:Amp") == "amp"
    assert icon_for_model("Sub Mini") == "submini"


def test_every_drawing_stays_inside_the_grid():
    for name in ICON_NAMES:
        runs = polylines(name)
        assert runs, f"{name} drew nothing"
        for run in runs:
            for x, y in run:
                assert -0.01 <= x <= VIEW + 0.01
                assert -0.01 <= y <= VIEW + 0.01


@pytest.mark.parametrize("size", ICON_SIZES)
def test_every_device_renders_a_png_of_the_right_size(size):
    for name in ICON_NAMES:
        data = render(name, size)
        assert png_size(data) == (size, size)


def test_drawings_differ_from_one_another():
    rendered = {name: render(name, 48) for name in ICON_NAMES}
    assert len(set(rendered.values())) == len(ICON_NAMES)


def test_an_unknown_name_renders_the_fallback():
    assert render("no-such-device", 48) == render("generic", 48)


def test_icon_list_advertises_every_size():
    xml = icon_list_xml("/dev/uuid:abc")
    for size in ICON_SIZES:
        assert f"<url>/dev/uuid:abc/icon/{size}.png</url>" in xml
        assert f"<width>{size}</width>" in xml
