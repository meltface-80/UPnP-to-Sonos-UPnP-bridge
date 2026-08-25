from sonosbridge import didl

AUDIRVANA_META = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
    '<item id="0" parentID="-1" restricted="1">'
    "<dc:title>So What</dc:title><dc:creator>Miles Davis</dc:creator>"
    "<upnp:album>Kind of Blue</upnp:album>"
    "<upnp:albumArtURI>http://10.0.0.5:8080/art.jpg</upnp:albumArtURI>"
    "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
    '<res protocolInfo="http-get:*:audio/x-flac:DLNA.ORG_OP=01" '
    'duration="0:09:22.000" sampleFrequency="96000" bitsPerSample="24">'
    "http://10.0.0.5:8080/1.flac</res></item></DIDL-Lite>"
)


def test_parses_the_fields_the_bridge_needs():
    meta = didl.parse(AUDIRVANA_META)
    assert meta.title == "So What"
    assert meta.display_artist == "Miles Davis"
    assert meta.album == "Kind of Blue"
    assert meta.album_art_uri == "http://10.0.0.5:8080/art.jpg"
    assert meta.duration == "0:09:22"


def test_rebuild_produces_sonos_shaped_metadata():
    out = didl.rebuild("http://10.0.0.5:8080/1.flac", AUDIRVANA_META)
    assert 'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/"' in out
    assert "RINCON_AssociatedZPUDN" in out  # Sonos rejects third-party items without it
    assert 'protocolInfo="http-get:*:audio/x-flac:*"' in out
    assert 'duration="0:09:22"' in out
    assert "<dc:title>So What</dc:title>" in out


def test_missing_metadata_still_yields_a_playable_item():
    out = didl.rebuild("http://10.0.0.5:8080/Some%20Song.wav", "")
    assert "<dc:title>Some Song</dc:title>" in out
    assert 'protocolInfo="http-get:*:audio/wav:*"' in out


def test_malformed_metadata_does_not_raise():
    assert didl.parse("<not xml").title == ""
    assert "item" in didl.rebuild("http://h/x.mp3", "<broken")


def test_special_characters_are_escaped():
    out = didl.rebuild(
        "http://h/a%26b.mp3",
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<item><dc:title>Rock &amp; Roll &lt;live&gt;</dc:title></item></DIDL-Lite>",
    )
    assert "Rock &amp; Roll &lt;live&gt;" in out
    from defusedxml import ElementTree as DET

    DET.fromstring(out)  # must stay well-formed


def test_protocol_info_falls_back_for_non_http_schemes():
    assert (
        didl.protocol_info_for("http://h/x.flac", "x-file-cifs:*:audio/flac:*")
        == "http-get:*:audio/flac:*"
    )
    assert didl.protocol_info_for("http://h/x.m4a", "") == "http-get:*:audio/mp4:*"
    assert didl.protocol_info_for("http://h/x", "") == "http-get:*:audio/mpeg:*"


def test_duration_normalisation():
    assert didl.normalise_duration("0:03:12.500") == "0:03:12"
    assert didl.normalise_duration("3:12") == "0:03:12"
    assert didl.normalise_duration("") == ""
    assert didl.normalise_duration("nonsense") == ""
