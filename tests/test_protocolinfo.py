from sonosbridge import protocolinfo

SONOS_SINK = (
    "http-get:*:audio/mpeg:*,http-get:*:audio/mp4:*,"
    "x-file-cifs:*:audio/mpeg:*,x-rincon-mp3radio:*:*:*,"
    "x-sonos-http:*:audio/mpeg:*,http-get:*:video/mp4:*,"
    "http-get:*:audio/x-sonos-recordable:*"
)


def test_only_http_audio_entries_survive():
    sink = protocolinfo.build_sink(SONOS_SINK).split(",")
    assert "http-get:*:audio/x-sonos-recordable:*" in sink
    assert not any(entry.startswith("x-file-cifs") for entry in sink)
    assert not any(entry.startswith("x-rincon") for entry in sink)
    assert not any("video/" in entry for entry in sink)


def test_the_formats_audirvana_cares_about_are_always_offered():
    sink = protocolinfo.build_sink("").split(",")
    for mime in ("audio/flac", "audio/x-flac", "audio/wav", "audio/aiff", "audio/mp4",
                 "audio/mpeg", "audio/aac"):
        assert f"http-get:*:{mime}:*" in sink


def test_no_duplicates_and_extras_are_appended():
    sink = protocolinfo.build_sink(SONOS_SINK, ["http-get:*:audio/flac:*",
                                                "http-get:*:audio/dsd:*"]).split(",")
    assert len(sink) == len(set(sink))
    assert sink[-1] == "http-get:*:audio/dsd:*"
