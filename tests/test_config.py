import pytest

from sonosbridge.config import Config


def test_defaults_are_sane():
    config = Config.from_env()
    assert config.mode == "queue"
    assert config.http_port == 1500
    assert config.name_suffix == " (Sonos)"


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("BRIDGE_MODE", "DIRECT")
    monkeypatch.setenv("HTTP_PORT", "8099")
    monkeypatch.setenv("NAME_SUFFIX", "")
    monkeypatch.setenv("EXCLUDE_ZONES", "Bathroom, Garage ")
    monkeypatch.setenv("UNGROUP_ON_PLAY", "yes")
    config = Config.from_env()
    assert config.mode == "direct"
    assert config.http_port == 8099
    assert config.name_suffix == ""
    assert config.exclude_zones == ["Bathroom", "Garage"]
    assert config.ungroup_on_play is True


def test_nonsense_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("BRIDGE_MODE", "telepathy")
    monkeypatch.setenv("HTTP_PORT", "not-a-port")
    config = Config.from_env()
    assert config.mode == "queue"
    assert config.http_port == 1500


@pytest.mark.parametrize(
    ("include", "exclude", "room", "expected"),
    [
        ([], [], "Kitchen", True),
        (["Kitchen"], [], "kitchen", True),
        (["Study"], [], "Kitchen", False),
        ([], ["Kitchen"], "Kitchen", False),
        (["Kitchen"], ["Kitchen"], "Kitchen", False),
    ],
)
def test_zone_filters(include, exclude, room, expected):
    config = Config(include_zones=include, exclude_zones=exclude)
    assert config.zone_allowed(room) is expected


def test_device_ids_are_stable_and_unique():
    config = Config()
    first = config.udn_for("RINCON_AAA01400")
    assert first == Config().udn_for("RINCON_AAA01400")
    assert first != config.udn_for("RINCON_BBB01400")
    assert first.startswith("uuid:")
