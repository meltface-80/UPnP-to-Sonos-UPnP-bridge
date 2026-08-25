import pytest

from sonosbridge.soap import (
    UPnPError,
    build_fault,
    build_request,
    build_response,
    parse_action,
    parse_response,
)

AVT = "urn:schemas-upnp-org:service:AVTransport:1"


def test_request_round_trip_preserves_embedded_xml():
    metadata = '<DIDL-Lite><item id="1"><dc:title>A & B</dc:title></item></DIDL-Lite>'
    envelope = build_request(AVT, "SetAVTransportURI", {
        "InstanceID": 0, "CurrentURI": "http://h/x.flac", "CurrentURIMetaData": metadata,
    })
    service, action, args = parse_action(envelope)
    assert service == AVT
    assert action == "SetAVTransportURI"
    assert args["CurrentURIMetaData"] == metadata


def test_response_round_trip():
    envelope = build_response(AVT, "GetPositionInfo", {"Track": "1", "RelTime": "0:00:12"})
    assert parse_response(envelope, "GetPositionInfo") == {"Track": "1", "RelTime": "0:00:12"}


def test_faults_become_typed_errors():
    with pytest.raises(UPnPError) as excinfo:
        parse_response(build_fault(701, "Transition not available"), "Play")
    assert excinfo.value.code == 701
    assert "Transition not available" in excinfo.value.description


def test_fault_code_gets_a_default_description():
    assert "<errorDescription>Action Failed</errorDescription>" in build_fault(501)


def test_a_sonos_style_fault_is_understood():
    body = (
        '<?xml version="1.0"?><s:Envelope '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
        "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
        '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>714</errorCode>'
        "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
    )
    with pytest.raises(UPnPError) as excinfo:
        parse_response(body, "SetAVTransportURI")
    assert excinfo.value.code == 714
    assert excinfo.value.description == "Illegal MIME-type"


def test_empty_body_is_an_error():
    with pytest.raises(UPnPError):
        parse_action(
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            "<s:Body></s:Body></s:Envelope>"
        )
