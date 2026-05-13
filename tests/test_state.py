"""Test state manager: ingest INDI events + snapshot + listener."""
from __future__ import annotations

import asyncio

import pytest

from astroarch_bridge.indi.protocol import IndiParser
from astroarch_bridge.state import StateManager


def parse_to_events(data: bytes) -> list:
    evs = []
    p = IndiParser(evs.append)
    p.feed(data)
    return evs


@pytest.mark.asyncio
async def test_def_then_set_updates_property():
    sm = StateManager()
    xml1 = (
        b'<defNumberVector device="EQMod" name="EQUATORIAL_EOD_COORD" state="Idle" perm="rw">'
        b'<defNumber name="RA">0</defNumber><defNumber name="DEC">0</defNumber>'
        b'</defNumberVector>'
    )
    xml2 = (
        b'<setNumberVector device="EQMod" name="EQUATORIAL_EOD_COORD" state="Ok">'
        b'<oneNumber name="RA">12.5</oneNumber><oneNumber name="DEC">41.3</oneNumber>'
        b'</setNumberVector>'
    )
    for ev in parse_to_events(xml1):
        await sm.handle_indi_event(ev)
    for ev in parse_to_events(xml2):
        await sm.handle_indi_event(ev)

    p = await sm.get_property("EQMod", "EQUATORIAL_EOD_COORD")
    assert p is not None
    elts = {e["name"]: e["value"] for e in p["elements"]}
    assert elts["RA"] == 12.5
    assert elts["DEC"] == 41.3
    assert p["state"] == "Ok"


@pytest.mark.asyncio
async def test_listener_receives_events():
    sm = StateManager()
    received = []

    async def listener(ev):
        received.append(ev)

    sm.add_listener(listener)
    xml = (
        b'<defSwitchVector device="X" name="S" state="Ok" perm="rw" rule="OneOfMany">'
        b'<defSwitch name="A">On</defSwitch></defSwitchVector>'
    )
    for ev in parse_to_events(xml):
        await sm.handle_indi_event(ev)
    assert any(e["type"] == "property_def" for e in received)


@pytest.mark.asyncio
async def test_snapshot_includes_all():
    sm = StateManager()
    xml = (
        b'<defTextVector device="A" name="MSG" state="Ok" perm="rw">'
        b'<defText name="T">hi</defText></defTextVector>'
    )
    for ev in parse_to_events(xml):
        await sm.handle_indi_event(ev)
    snap = await sm.snapshot()
    assert "A" in snap["devices"]
    assert any(p["name"] == "MSG" for p in snap["properties"])
