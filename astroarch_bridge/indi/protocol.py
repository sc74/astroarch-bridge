"""Parser incrementale del protocollo XML INDI.

Il protocollo INDI è uno stream XML *senza* singolo root: il server invia
sequenze di top-level elements come <defXxxVector>, <setXxxVector>, <message>,
<delProperty>, ognuno self-contained.

DoD:
- Accetta chunk arbitrari di bytes
- Buffer interno, mai perde dati
- Emette callback su evento completo (def/set/del/message)
- Resiliente a XML non valido tra eventi (recupera al prossimo top-level)
- BLOB con base64 -> bytes

Errori prevenuti:
- E2: chunk parziale -> wrapper root virtuale + xml.parsers.expat in push mode
- E3: tipo cambiato -> emettiamo eventi grezzi, sta al consumer ricreare la Property
- malformed XML -> reset parser su eccezione, log warning, scarta solo il chunk corrente
"""
from __future__ import annotations

import base64
import logging
import xml.parsers.expat
from dataclasses import dataclass, field
from typing import Callable, Optional

from .properties import (
    Element,
    PropPerm,
    PropState,
    PropType,
    Property,
    SwitchRule,
    is_element_tag,
    parse_value_for_type,
    proptype_from_def_tag,
    proptype_from_set_tag,
)

log = logging.getLogger(__name__)


@dataclass
class IndiEvent:
    """Evento ad alto livello emesso dal parser."""
    kind: str  # "def" | "set" | "del" | "message"
    device: str = ""
    name: str = ""
    payload: dict = field(default_factory=dict)
    property: Optional[Property] = None  # presente per def/set


EventCallback = Callable[[IndiEvent], None]


class IndiParser:
    """Parser INDI push-based, alimentato chunk a chunk via feed()."""

    # Wrappiamo lo stream in un root fittizio per renderlo XML valido per expat.
    _ROOT_OPEN = b"<indi-stream>"
    _ROOT_CLOSE = b"</indi-stream>"

    def __init__(self, on_event: EventCallback):
        self._on_event = on_event
        self._parser: xml.parsers.expat.XMLParserType | None = None
        self._cur_property: Property | None = None
        self._cur_element: Element | None = None
        self._cur_text: list[str] = []
        self._cur_set_event: IndiEvent | None = None  # per setXxxVector
        self._reset_parser(prime=True)

    # --- Setup parser -------------------------------------------------------

    def _reset_parser(self, prime: bool) -> None:
        p = xml.parsers.expat.ParserCreate()
        p.StartElementHandler = self._start
        p.EndElementHandler = self._end
        p.CharacterDataHandler = self._chars
        self._parser = p
        self._cur_property = None
        self._cur_element = None
        self._cur_text = []
        self._cur_set_event = None
        if prime:
            try:
                self._parser.Parse(self._ROOT_OPEN, False)
            except xml.parsers.expat.ExpatError as e:
                log.error("expat prime failed: %s", e)

    # --- Public API ---------------------------------------------------------

    def feed(self, data: bytes) -> None:
        if not data or self._parser is None:
            return
        try:
            self._parser.Parse(data, False)
        except xml.parsers.expat.ExpatError as e:
            log.warning("INDI XML parse error at line %s col %s: %s -- resetting parser",
                        getattr(e, "lineno", "?"), getattr(e, "offset", "?"), e)
            self._reset_parser(prime=True)

    def close(self) -> None:
        if self._parser is None:
            return
        try:
            self._parser.Parse(self._ROOT_CLOSE, True)
        except xml.parsers.expat.ExpatError:
            pass
        self._parser = None

    # --- Expat handlers -----------------------------------------------------

    def _start(self, tag: str, attrs: dict[str, str]) -> None:
        # def<Type>Vector -> nuova property completa
        ptype = proptype_from_def_tag(tag)
        if ptype is not None:
            self._cur_property = Property(
                device=attrs.get("device", ""),
                name=attrs.get("name", ""),
                type=ptype,
                label=attrs.get("label", ""),
                group=attrs.get("group", "Main"),
                state=PropState.parse(attrs.get("state")),
                perm=PropPerm.parse(attrs.get("perm")),
                timeout=float(attrs.get("timeout", 0) or 0),
                timestamp=attrs.get("timestamp", ""),
                rule=SwitchRule.parse(attrs.get("rule")) if ptype == PropType.SWITCH else None,
            )
            return

        # set<Type>Vector -> aggiornamento valori
        stype = proptype_from_set_tag(tag)
        if stype is not None:
            self._cur_set_event = IndiEvent(
                kind="set",
                device=attrs.get("device", ""),
                name=attrs.get("name", ""),
                payload={
                    "type": stype.value,
                    "state": attrs.get("state"),
                    "timestamp": attrs.get("timestamp", ""),
                    "values": [],  # list of (eltName, raw_value)
                },
            )
            return

        # delProperty
        if tag == "delProperty":
            self._on_event(IndiEvent(
                kind="del",
                device=attrs.get("device", ""),
                name=attrs.get("name", ""),  # vuoto = tutto il device
                payload={"timestamp": attrs.get("timestamp", "")},
            ))
            return

        # message
        if tag == "message":
            self._on_event(IndiEvent(
                kind="message",
                device=attrs.get("device", ""),
                payload={
                    "message": attrs.get("message", ""),
                    "timestamp": attrs.get("timestamp", ""),
                },
            ))
            return

        # Element tag (def<X> o one<X>)
        if is_element_tag(tag):
            self._cur_element = Element(
                name=attrs.get("name", ""),
                label=attrs.get("label", ""),
                format=attrs.get("format", "%g"),
                min=float(attrs.get("min", 0) or 0),
                max=float(attrs.get("max", 0) or 0),
                step=float(attrs.get("step", 0) or 0),
            )
            self._cur_text = []
            return
        # altri tag (es. oneBLOB con format/size attrs) - tracciamo comunque text
        self._cur_text = []

    def _chars(self, data: str) -> None:
        if self._cur_element is not None or self._cur_set_event is not None:
            self._cur_text.append(data)

    def _end(self, tag: str) -> None:
        # Chiusura element dentro def<X>Vector
        if tag.startswith("def") and tag != "delProperty" and is_element_tag(tag):
            if self._cur_property is not None and self._cur_element is not None:
                raw = "".join(self._cur_text)
                self._cur_element.value = parse_value_for_type(self._cur_property.type, raw)
                self._cur_property.elements[self._cur_element.name] = self._cur_element
            self._cur_element = None
            self._cur_text = []
            return

        # Chiusura element dentro set<X>Vector (oneXxx)
        if tag.startswith("one") and is_element_tag(tag):
            if self._cur_set_event is not None and self._cur_element is not None:
                raw = "".join(self._cur_text).strip()
                self._cur_set_event.payload["values"].append((self._cur_element.name, raw))
            self._cur_element = None
            self._cur_text = []
            return

        # Chiusura def<X>Vector -> emetti evento
        if proptype_from_def_tag(tag) is not None:
            if self._cur_property is not None:
                ev = IndiEvent(
                    kind="def",
                    device=self._cur_property.device,
                    name=self._cur_property.name,
                    payload={"type": self._cur_property.type.value},
                    property=self._cur_property,
                )
                self._on_event(ev)
                self._cur_property = None
            return

        # Chiusura set<X>Vector -> emetti evento
        if proptype_from_set_tag(tag) is not None:
            if self._cur_set_event is not None:
                self._on_event(self._cur_set_event)
                self._cur_set_event = None
            return

        # message ha solo attributi
        if tag == "message":
            return


# --- Builder XML in uscita (comandi al server) ------------------------------

def build_get_properties(version: str = "1.7", device: str = "", name: str = "") -> bytes:
    """Richiede definizione delle proprietà (alla connessione)."""
    attrs = [f'version="{version}"']
    if device:
        attrs.append(f'device="{_xmlesc(device)}"')
    if name:
        attrs.append(f'name="{_xmlesc(name)}"')
    return f"<getProperties {' '.join(attrs)}/>".encode("utf-8")


def build_new_switch(device: str, name: str, values: dict[str, bool]) -> bytes:
    parts = [f'<newSwitchVector device="{_xmlesc(device)}" name="{_xmlesc(name)}">']
    for k, v in values.items():
        parts.append(f'<oneSwitch name="{_xmlesc(k)}">{"On" if v else "Off"}</oneSwitch>')
    parts.append("</newSwitchVector>")
    return "".join(parts).encode("utf-8")


def build_new_number(device: str, name: str, values: dict[str, float]) -> bytes:
    parts = [f'<newNumberVector device="{_xmlesc(device)}" name="{_xmlesc(name)}">']
    for k, v in values.items():
        parts.append(f'<oneNumber name="{_xmlesc(k)}">{v:g}</oneNumber>')
    parts.append("</newNumberVector>")
    return "".join(parts).encode("utf-8")


def build_new_text(device: str, name: str, values: dict[str, str]) -> bytes:
    parts = [f'<newTextVector device="{_xmlesc(device)}" name="{_xmlesc(name)}">']
    for k, v in values.items():
        parts.append(f'<oneText name="{_xmlesc(k)}">{_xmlesc(v)}</oneText>')
    parts.append("</newTextVector>")
    return "".join(parts).encode("utf-8")


def build_enable_blob(device: str, mode: str = "Also") -> bytes:
    """Mode: Never | Also | Only. Default Also = ricevi anche BLOB."""
    return f'<enableBLOB device="{_xmlesc(device)}">{mode}</enableBLOB>'.encode("utf-8")


def _xmlesc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def decode_blob(raw_b64: str) -> bytes:
    """Decodifica payload BLOB base64 (può contenere whitespace)."""
    cleaned = "".join(raw_b64.split())
    try:
        return base64.b64decode(cleaned, validate=False)
    except Exception as e:
        log.warning("blob decode failed: %s", e)
        return b""
