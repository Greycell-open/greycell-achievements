"""Fireproof Studios saves (The Room series), read into plain values.

The Room 4: Old Sins keeps one file per slot, `<slot>.save` in
`LocalLow/Fireproof Studios/Old Sins`: a 24-byte header (format version 2 and
five sizes), then one LZF stream. Unpacked, that is two XML documents back to
back: the `PlayerSlot` (slot name, `GameComplete`, play time) and an
`ArrayOfSceneMemoryStream` whose scenes are themselves XML, base64-encoded.
The hub scene's sub-level manager keeps `TrackedLevelStates`, one per room:
Locked, Entered or Complete. That table is what the achievements follow.

Read into:

    {"slot": {"SlotName": "z", "GameComplete": "false", ...},
     "levels": {"FOY_Main": "Complete", "KIT_Main": "Entered", ...}}

so the dotted `field` rules for JSON saves apply (`levels.FOY_Main`). The
text form, for `contains` rules, is both XML documents plus every scene.

Read-only and bounded: a malformed file raises FireproofError, the unpacked
size is capped, and XML with a document type (entity tricks) is refused.
"""
from __future__ import annotations

import base64
import binascii
import struct
import xml.etree.ElementTree as ET

VERSION = 2
HEADER = 24
MAX_UNPACKED = 64 * 1024 * 1024
_BOM = "﻿"


class FireproofError(ValueError):
    pass


def unlzf(data: bytes, start: int = 0, limit: int = MAX_UNPACKED) -> bytes:
    """liblzf's format: a control byte below 32 copies that many plus one
    literal bytes; any other is a back reference of length (c >> 5) + 2
    (7 means one more length byte) at distance ((c & 31) << 8 | next) + 1."""
    out = bytearray()
    i, end = start, len(data)
    while i < end:
        c = data[i]
        i += 1
        if c < 32:
            n = c + 1
            if i + n > end:
                raise FireproofError("literal run past the end of the file")
            out += data[i:i + n]
            i += n
        else:
            length = c >> 5
            if length == 7:
                if i >= end:
                    raise FireproofError("back reference past the end of the file")
                length += data[i]
                i += 1
            if i >= end:
                raise FireproofError("back reference past the end of the file")
            ref = len(out) - ((c & 0x1F) << 8) - data[i] - 1
            i += 1
            length += 2
            if ref < 0:
                raise FireproofError("back reference before the start of the data")
            if ref + length <= len(out):
                out += out[ref:ref + length]
            else:                               # overlapping: repeats what it is still writing
                for k in range(length):
                    out.append(out[ref + k])
        if len(out) > limit:
            raise FireproofError(f"unpacks to more than {limit // (1024 * 1024)} MB")
    return bytes(out)


def _xml(text: str) -> ET.Element:
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise FireproofError("XML with a document type is not a save")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise FireproofError(f"not XML ({exc})") from None


def _documents(text: str) -> list[str]:
    """The XML documents in the unpacked text, each from its declaration."""
    # Each document but the last ends with the next one's byte-order mark.
    return ["<?xml" + part.rstrip().rstrip(_BOM).rstrip() for part in text.split("<?xml")[1:]]


def parse(data: bytes) -> tuple[dict, str]:
    """(values, text) from a whole `.save` file."""
    if len(data) <= HEADER:
        raise FireproofError("too short to be a save")
    version = struct.unpack_from("<I", data, 0)[0]
    if version != VERSION:
        raise FireproofError(f"save format {version}, this reads {VERSION}")
    text = unlzf(data, HEADER).decode("utf-8", "replace")
    docs = _documents(text)
    slot = next((d for d in docs if "<PlayerSlot" in d), None)
    if slot is None:
        raise FireproofError("no PlayerSlot in the save")
    root = _xml(slot)
    values = {"slot": {child.tag: (child.text or "") for child in root if len(child) == 0}, "levels": {}}
    texts = list(docs)
    for doc in docs:
        if "<ArrayOfSceneMemoryStream" not in doc:
            continue
        for scene in _xml(doc).iter("SceneMemoryStream"):
            try:
                inner = base64.b64decode(scene.findtext("ByteArray") or "", validate=True)
            except (binascii.Error, ValueError):
                raise FireproofError(f"scene {scene.findtext('SceneName')!r} is not base64") from None
            inner_text = inner.decode("utf-8", "replace").lstrip(_BOM)
            texts.append(inner_text)
            if "TrackedLevelStates" not in inner_text:
                continue
            for item in _xml(inner_text).iter("TrackedLevelStates"):
                for entry in item.iter("item"):
                    name, state = entry.findtext("key/string"), entry.findtext("value/LevelState")
                    if name and state:
                        values["levels"][name] = state
    return values, "\n".join(texts)
