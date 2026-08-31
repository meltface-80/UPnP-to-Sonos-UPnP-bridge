"""The drawings themselves: one projection, one grid, nothing off the page."""

from __future__ import annotations

import pytest

from sonosbridge import deviceicons


@pytest.mark.parametrize("name", deviceicons.ICON_NAMES)
def test_every_device_draws_something_inside_the_grid(name):
    runs = deviceicons.polylines(name)
    assert runs, f"{name} drew nothing"
    for run in runs:
        assert len(run) >= 2
        for x, y in run:
            assert -0.01 <= x <= deviceicons.VIEW + 0.01
            assert -0.01 <= y <= deviceicons.VIEW + 0.01


def _span(name: str) -> float:
    runs = deviceicons.polylines(name)
    xs = [x for run in runs for x, _ in run]
    ys = [y for run in runs for _, y in run]
    return max(max(xs) - min(xs), max(ys) - min(ys))


@pytest.mark.parametrize("name", deviceicons.ICON_NAMES)
def test_every_device_fills_the_grid_it_is_given(name):
    """Fitting should use the space: a drawing that hugs the centre reads as a dot."""
    room = deviceicons.VIEW - 2 * deviceicons.MARGIN
    assert deviceicons.RELATIVE.get(name, 1.0) * room == pytest.approx(_span(name), abs=0.05)
    assert _span(name) > room * 0.75


def test_a_family_keeps_its_pecking_order():
    """An Arc is longer than a Beam, which is longer than a Ray."""
    assert _span("arc") > _span("playbar") > _span("beam") > _span("ray")
    assert _span("five") > _span("play3")
    assert _span("sub") > _span("submini")


@pytest.mark.parametrize("name", deviceicons.ICON_NAMES)
def test_svg_paths_are_emitted_for_every_stroke(name):
    paths = deviceicons.svg_paths(name)
    assert len(paths) == len(deviceicons.fitted(name))
    assert all(d.startswith("M") for d in paths)


def test_unknown_devices_fall_back_rather_than_raising():
    assert deviceicons.polylines("no-such-device") == deviceicons.polylines(deviceicons.FALLBACK)


def test_the_bars_keep_enough_height_to_read_as_objects():
    """A soundbar is mostly length; its depth is what stops it being a line."""
    for name in ("arc", "beam", "ray", "playbar", "playbase"):
        runs = deviceicons.polylines(name)
        ys = [y for run in runs for _, y in run]
        assert max(ys) - min(ys) > deviceicons.VIEW * 0.12
