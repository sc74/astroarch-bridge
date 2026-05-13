"""Modelli delle proprietà INDI.

DoD:
- Una classe per tipo INDI (Number, Switch, Text, Light, BLOB)
- Serializzabili a dict per WS/REST
- Gestione perm/state/group/label
- Mai fail su campi mancanti (default sicuri)

Errori prevenuti:
- E3: property che cambia tipo runtime -> usiamo factory che ricrea la property se cambia tipo
- BLOB pesanti -> escludibili dalla serializzazione standard
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PropType(str, Enum):
    NUMBER = "Number"
    SWITCH = "Switch"
    TEXT = "Text"
    LIGHT = "Light"
    BLOB = "BLOB"


class PropState(str, Enum):
    IDLE = "Idle"
    OK = "Ok"
    BUSY = "Busy"
    ALERT = "Alert"

    @classmethod
    def parse(cls, v: str | None) -> "PropState":
        if not v:
            return cls.IDLE
        v_low = v.strip().lower()
        for s in cls:
            if s.value.lower() == v_low:
                return s
        return cls.IDLE


class PropPerm(str, Enum):
    RO = "ro"
    WO = "wo"
    RW = "rw"

    @classmethod
    def parse(cls, v: str | None) -> "PropPerm":
        v_low = (v or "ro").strip().lower()
        for p in cls:
            if p.value == v_low:
                return p
        return cls.RO


class SwitchRule(str, Enum):
    ONE_OF_MANY = "OneOfMany"
    AT_MOST_ONE = "AtMostOne"
    ANY_OF_MANY = "AnyOfMany"

    @classmethod
    def parse(cls, v: str | None) -> "SwitchRule":
        v_low = (v or "AnyOfMany").strip().lower()
        for r in cls:
            if r.value.lower() == v_low:
                return r
        return cls.ANY_OF_MANY


@dataclass
class Element:
    """Singolo elemento dentro una property vector."""
    name: str
    label: str = ""
    value: Any = None
    # Solo per Number:
    format: str = "%g"
    min: float = 0.0
    max: float = 0.0
    step: float = 0.0

    def to_dict(self, include_blob_bytes: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "label": self.label or self.name,
        }
        # BLOB: omesso il payload binario di default
        if isinstance(self.value, (bytes, bytearray, memoryview)):
            if include_blob_bytes:
                d["value"] = bytes(self.value).hex()
                d["size"] = len(self.value)
            else:
                d["value"] = None
                d["size"] = len(self.value)
        else:
            d["value"] = self.value
        if self.format != "%g" or self.min or self.max or self.step:
            d["format"] = self.format
            d["min"] = self.min
            d["max"] = self.max
            d["step"] = self.step
        return d


@dataclass
class Property:
    """Una property INDI completa (vector con N elementi)."""
    device: str
    name: str
    type: PropType
    label: str = ""
    group: str = ""
    state: PropState = PropState.IDLE
    perm: PropPerm = PropPerm.RO
    timeout: float = 0.0
    timestamp: str = ""
    rule: SwitchRule | None = None  # solo per Switch
    elements: dict[str, Element] = field(default_factory=dict)
    last_update: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return f"{self.device}::{self.name}"

    def to_dict(self, include_blob_bytes: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "device": self.device,
            "name": self.name,
            "type": self.type.value,
            "label": self.label or self.name,
            "group": self.group or "Main",
            "state": self.state.value,
            "perm": self.perm.value,
            "timestamp": self.timestamp,
            "elements": [e.to_dict(include_blob_bytes) for e in self.elements.values()],
            "last_update": self.last_update,
        }
        if self.rule is not None:
            d["rule"] = self.rule.value
        return d

    def update_state(self, state: str | None, timestamp: str | None) -> None:
        if state is not None:
            self.state = PropState.parse(state)
        if timestamp:
            self.timestamp = timestamp
        self.last_update = time.time()

    def update_value(self, name: str, value: Any) -> bool:
        """Aggiorna il valore di un elemento. Ritorna True se l'elemento esisteva."""
        elt = self.elements.get(name)
        if elt is None:
            return False
        elt.value = value
        return True


# --- Factory helpers --------------------------------------------------------

_TYPE_BY_DEFTAG = {
    "defNumberVector": PropType.NUMBER,
    "defSwitchVector": PropType.SWITCH,
    "defTextVector": PropType.TEXT,
    "defLightVector": PropType.LIGHT,
    "defBLOBVector": PropType.BLOB,
}

_ELT_TAGS = {
    "defNumber", "defSwitch", "defText", "defLight", "defBLOB",
    "oneNumber", "oneSwitch", "oneText", "oneLight", "oneBLOB",
}


def proptype_from_def_tag(tag: str) -> PropType | None:
    return _TYPE_BY_DEFTAG.get(tag)


def proptype_from_set_tag(tag: str) -> PropType | None:
    mapping = {
        "setNumberVector": PropType.NUMBER,
        "setSwitchVector": PropType.SWITCH,
        "setTextVector": PropType.TEXT,
        "setLightVector": PropType.LIGHT,
        "setBLOBVector": PropType.BLOB,
    }
    return mapping.get(tag)


def is_element_tag(tag: str) -> bool:
    return tag in _ELT_TAGS


def parse_value_for_type(t: PropType, raw: str) -> Any:
    raw = (raw or "").strip()
    if t == PropType.NUMBER:
        try:
            # INDI può mandare anche sessagesimi tipo "12:34:56"
            if ":" in raw:
                parts = raw.split(":")
                acc = 0.0
                sign = -1.0 if parts[0].startswith("-") else 1.0
                parts[0] = parts[0].lstrip("+-")
                for i, p in enumerate(parts):
                    acc += float(p) / (60 ** i)
                return sign * acc
            return float(raw) if raw else 0.0
        except ValueError:
            return 0.0
    if t == PropType.SWITCH:
        return raw.lower() == "on"
    if t == PropType.LIGHT:
        return raw  # "Idle"|"Ok"|"Busy"|"Alert"
    return raw  # TEXT
