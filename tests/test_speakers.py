"""Model recognition and the line-art glyphs drawn from it."""

from __future__ import annotations

import struct

import pytest

from sonosbridge import icon, speakers
from sonosbridge.sonos import ZoneInfo, parse_channel_map

# The names Sonos players actually report in <modelName>.
MODELS = {
    "Sonos Five": "five",
    "Sonos PLAY:5": "five",
    "Sonos PLAY:3": "play3",
    "Sonos PLAY:1": "play1",
    "Sonos One": "one",
    "Sonos One SL": "one",
    "Sonos Era 100": "era100",
    "Sonos Era 300": "era300",
    "Sonos Beam": "beam",
    "Sonos Arc": "arc",
    "Sonos Arc Ultra": "arc",
    "Sonos Ray": "ray",
    "Sonos PLAYBAR": "playbar",
    "Sonos PLAYBASE": "playbase",
    "Sonos Move": "move",
    "Sonos Move 2": "move",
    "Sonos Roam": "roam",
    "Sonos Roam SL": "roam",
    "Sonos SUB": "sub",
    "Sonos Sub Mini": "submini",
    "Sonos Amp": "amp",
    "Sonos CONNECT:AMP": "amp",
    "Sonos Port": "port",
    "Sonos CONNECT": "port",
    "ZP120": "amp",
    "ZP90": "port",
    "SYMFONISK Bookshelf speaker": "bookshelf",
    "SYMFONISK Table lamp with WiFi speaker": "lamp",
    "SYMFONISK Picture frame with Wi-Fi speaker": "frame",
}


@pytest.mark.parametrize(("model", "kind"), sorted(MODELS.items()))
def test_known_models_get_their_own_glyph(model, kind):
    assert speakers.classify(model) == kind


@pytest.mark.parametrize("model", ["", "   ", "Sonos Something New", "Acme Speaker"])
def test_unknown_models_fall_back_to_a_generic_speaker(model):
    assert speakers.classify(model) == speakers.DEFAULT_KIND


def test_longer_names_win_over_the_words_inside_them():
    # "Sub Mini" must not match "Sub", "Connect:Amp" must not match "Connect".
    assert speakers.classify("Sonos Sub Mini") != speakers.classify("Sonos SUB")
    assert speakers.classify("Sonos CONNECT:AMP") != speakers.classify("Sonos CONNECT")
    assert speakers.classify("Sonos Era 300") != speakers.classify("Sonos Era 100")


@pytest.mark.parametrize("kind", speakers.KINDS)
def test_every_glyph_is_drawable_and_stays_inside_the_box(kind):
    for paths in (speakers.glyph(kind), speakers.glyph(kind, pair=True)):
        assert paths
        x0, y0, x1, y1 = speakers._bbox(paths)
        assert x0 >= 0 and y0 >= 0 and x1 <= speakers.VIEWBOX and y1 <= speakers.VIEWBOX
        assert all(len(path) >= 2 for path in paths)


@pytest.mark.parametrize("kind", speakers.KINDS)
def test_a_stereo_pair_draws_two_of_the_same_speaker(kind):
    single = speakers.glyph(kind)
    pair = speakers.glyph(kind, pair=True)
    assert len(pair) == 2 * len(single)
    # Two copies, one either side of the centre line.
    left = speakers._bbox(pair[: len(single)])
    right = speakers._bbox(pair[len(single) :])
    assert left[2] < right[0]  # they do not overlap
    assert left[1] == pytest.approx(right[1])  # and sit at the same height


def test_svg_is_self_contained_and_follows_the_page_colour():
    markup = speakers.svg("beam", size=40)
    assert markup.startswith("<svg") and markup.endswith("</svg>")
    assert 'viewBox="0 0 100 100"' in markup
    assert 'stroke="currentColor"' in markup
    assert markup.count("<path") == len(speakers.glyph("beam"))

    standalone = speakers.svg("beam", auto_theme=True)
    assert "prefers-color-scheme" in standalone


def test_svg_labels_the_speaker_for_screen_readers():
    assert 'aria-label="Era 300"' in speakers.svg("era300")
    assert "<title>Kitchen</title>" in speakers.svg("one", title="Kitchen")


# ----------------------------------------------------------------------
# PNGs
# ----------------------------------------------------------------------
def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


@pytest.mark.parametrize("size", icon.ICON_SIZES)
def test_icons_render_at_the_advertised_size(size):
    assert _png_size(icon.render(size, "five")) == (size, size)


def test_each_model_gets_a_different_picture():
    drawn = {kind: icon.render(48, kind) for kind in speakers.KINDS}
    assert len(set(drawn.values())) == len(drawn)


def test_a_stereo_pair_looks_different_from_a_single_speaker():
    assert icon.render(48, "five") != icon.render(48, "five", pair=True)


def test_icons_can_be_rendered_straight_from_a_model_name():
    assert icon.render_for_model(48, "Sonos Beam") == icon.render(48, "beam")
    assert icon.render_for_model(48, "Nothing Like It") == icon.render(48, "generic")


# ----------------------------------------------------------------------
# Which rooms are pairs
# ----------------------------------------------------------------------
def test_channel_map_is_split_per_player():
    assert parse_channel_map("RINCON_A:LF,LF;RINCON_B:RF,RF") == {
        "RINCON_A": {"LF"},
        "RINCON_B": {"RF"},
    }
    assert parse_channel_map("") == {}
    assert parse_channel_map("nonsense") == {}


def test_a_bonded_pair_is_a_pair_but_a_bonded_sub_is_not():
    pair = ZoneInfo("A", "Lounge", "1.2.3.4", channel_map="RINCON_A:LF,LF;RINCON_B:RF,RF")
    with_sub = ZoneInfo("A", "Den", "1.2.3.4", channel_map="RINCON_A:LF,RF;RINCON_S:SW,SW")
    alone = ZoneInfo("A", "Kitchen", "1.2.3.4")

    assert pair.stereo_pair
    assert not with_sub.stereo_pair
    assert not alone.stereo_pair


def test_a_zone_knows_which_glyph_it_wants():
    assert ZoneInfo("A", "Lounge", "1.2.3.4", model="Sonos Five").icon_kind == "five"
    assert ZoneInfo("A", "Lounge", "1.2.3.4").icon_kind == speakers.DEFAULT_KIND
