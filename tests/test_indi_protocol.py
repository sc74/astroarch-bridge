"""Test parser INDI: chunk parziali, def/set/del/message, BLOB."""
from __future__ import annotations

from astroarch_bridge.indi.protocol import IndiEvent, IndiParser, decode_blob


def collect(chunks: list[bytes]) -> list[IndiEvent]:
    events: list[IndiEvent] = []
    parser = IndiParser(events.append)
    for c in chunks:
        parser.feed(c)
    return events


def test_def_number_vector_basic():
    xml = (
        b'<defNumberVector device="EQMod" name="EQUATORIAL_EOD_COORD" '
        b'label="Eq Coords" group="Main" state="Idle" perm="rw" timeout="60">'
        b'<defNumber name="RA" label="RA" format="%10.6m" min="0" max="24" step="0">12.5</defNumber>'
        b'<defNumber name="DEC" label="DEC" format="%10.6m" min="-90" max="90" step="0">41.3</defNumber>'
        b'</defNumberVector>'
    )
    events = collect([xml])
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "def"
    assert ev.device == "EQMod"
    assert ev.name == "EQUATORIAL_EOD_COORD"
    assert ev.property is not None
    assert "RA" in ev.property.elements
    assert ev.property.elements["RA"].value == 12.5
    assert ev.property.elements["DEC"].value == 41.3


def test_set_number_vector_updates():
    xml1 = (
        b'<defNumberVector device="EQMod" name="EQUATORIAL_EOD_COORD" state="Idle" perm="rw">'
        b'<defNumber name="RA">0</defNumber><defNumber name="DEC">0</defNumber>'
        b'</defNumberVector>'
    )
    xml2 = (
        b'<setNumberVector device="EQMod" name="EQUATORIAL_EOD_COORD" state="Ok">'
        b'<oneNumber name="RA">5.5</oneNumber><oneNumber name="DEC">10.0</oneNumber>'
        b'</setNumberVector>'
    )
    events = collect([xml1, xml2])
    assert events[0].kind == "def"
    assert events[1].kind == "set"
    assert events[1].payload["state"] == "Ok"
    assert ("RA", "5.5") in events[1].payload["values"]


def test_chunk_split_in_middle():
    xml = (
        b'<defSwitchVector device="X" name="ONOFF" state="Ok" perm="rw" rule="OneOfMany">'
        b'<defSwitch name="ON">On</defSwitch><defSwitch name="OFF">Off</defSwitch>'
        b'</defSwitchVector>'
    )
    # split at byte 30
    a, b = xml[:30], xml[30:]
    events = collect([a, b])
    assert len(events) == 1
    assert events[0].property.elements["ON"].value is True
    assert events[0].property.elements["OFF"].value is False


def test_message_event():
    xml = b'<message device="EQMod" message="hello" timestamp="2026-05-03T22:00:00"/>'
    events = collect([xml])
    assert events[0].kind == "message"
    assert events[0].payload["message"] == "hello"


def test_del_property_event():
    xml = b'<delProperty device="EQMod" name="ABORT_MOTION"/>'
    events = collect([xml])
    assert events[0].kind == "del"
    assert events[0].name == "ABORT_MOTION"


def test_malformed_xml_recovery():
    # Frame valido, poi rumore, poi altro frame valido
    bad = b'<<<not>xml'
    good_after = (
        b'<defTextVector device="X" name="HELLO" state="Ok" perm="rw">'
        b'<defText name="TXT">world</defText></defTextVector>'
    )
    parser_events: list[IndiEvent] = []
    parser = IndiParser(parser_events.append)
    parser.feed(bad)
    parser.feed(good_after)
    # Almeno il secondo deve essere stato parsato
    assert any(ev.kind == "def" and ev.name == "HELLO" for ev in parser_events)


def test_decode_blob_with_whitespace():
    import base64
    raw = b"hello world"
    encoded = base64.b64encode(raw).decode()
    # aggiungi whitespace
    encoded_ws = "\n  ".join(encoded[i:i+4] for i in range(0, len(encoded), 4))
    assert decode_blob(encoded_ws) == raw
