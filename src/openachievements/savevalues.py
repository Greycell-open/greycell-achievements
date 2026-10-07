"""Any save file as flat values, for comparing one save with the next.

`sniff` names a save's format from its content (an `.sav` may be JSON, an
Unreal save or a .NET one); `decode` reads it with the same readers save rules
use; `flatten` turns the result into {dotted field: value}, the same dotted
paths a rule's `field` takes, so a change seen here is a rule away.

A JSON save may be one document per line (Godot games often write that); it
reads as a list of them. Easy Save 3 (`.es3`) writes JSON where each value is wrapped as
{"__type": ..., "value": ...}; `es3` unwraps it.

Bounded: files above MAX_BYTES and more than MAX_FIELDS values are cut, and a
file no reader understands is "binary", compared only by its hash.
"""
from __future__ import annotations

import configparser
import json

MAX_BYTES = 64 * 1024 * 1024
MAX_FIELDS = 20000
FORMATS = ("fireproof", "gvas", "nrbf", "es3", "json", "ini")


def sniff(name: str, data: bytes) -> str:
    """The format a save rule would use for this file, or "binary"."""
    from . import nrbf
    if data[:4] == b"GVAS":
        return "gvas"
    if len(data) > 24 and data[:4] == b"\x02\x00\x00\x00" and data[24:25] == b"\x1f":
        return "fireproof"
    if nrbf.is_nrbf(data):
        return "nrbf"
    text = data[:4096].decode("utf-8", "replace").lstrip("﻿ \t\r\n")
    if text[:1] in "{[":
        if name.lower().endswith(".es3") or '"__type"' in text:
            return "es3"
        return "json"
    if name.lower().endswith((".ini", ".cfg")) or (text[:1] == "[" and "]\n" in text.replace("\r", "")):
        return "ini"
    return "binary"


def unwrap_es3(value):
    if isinstance(value, dict):
        if "value" in value and set(value) <= {"__type", "value"}:
            return unwrap_es3(value["value"])
        return {k: unwrap_es3(v) for k, v in value.items() if k != "__type"}
    if isinstance(value, list):
        return [unwrap_es3(v) for v in value]
    return value


def decode(fmt: str, data: bytes) -> object:
    """The parsed save; ValueError when it is not what `fmt` says."""
    text = lambda: data.decode("utf-8", "replace").lstrip("﻿")   # noqa: E731
    if fmt == "gvas":
        from .gvas import parse
        return parse(data)
    if fmt == "fireproof":
        from .fireproof import parse
        return parse(data)[0]
    if fmt == "nrbf":
        from .nrbf import parse
        return parse(data)
    if fmt in ("json", "es3"):
        try:
            value = json.loads(text())
        except ValueError:
            # Godot and others write one JSON document per line: a list of them.
            lines = [ln for ln in text().splitlines() if ln.strip()]
            if len(lines) < 2:
                raise
            value = [json.loads(ln) for ln in lines]
        return unwrap_es3(value) if fmt == "es3" else value
    if fmt == "ini":
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        raw = text()
        parser.read_string(raw if raw.lstrip().startswith("[") else "[root]\n" + raw)
        values = {name: dict(parser[name]) for name in parser.sections()}
        values.update(values.pop("root", {}))
        return values
    raise ValueError(f"no reader for {fmt}")


def flatten(value, prefix: str = "", out: dict | None = None) -> dict:
    """{dotted field: scalar}. Lists by position, as rule fields address them."""
    out = {} if out is None else out
    if len(out) >= MAX_FIELDS:
        return out
    if isinstance(value, dict):
        for k, v in value.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(value, list):
        for n, v in enumerate(value):
            flatten(v, f"{prefix}.{n}" if prefix else str(n), out)
    elif isinstance(value, (str, int, float, bool)) or value is None:
        out[prefix] = value
    return out


def values(name: str, data: bytes) -> tuple[str, dict]:
    """(format, flat values) for one save file; binary or unreadable files
    give an empty dict and their format ("binary" when no reader fits)."""
    if len(data) > MAX_BYTES:
        return "binary", {}
    fmt = sniff(name, data)
    if fmt == "binary":
        return fmt, {}
    try:
        return fmt, flatten(decode(fmt, data))
    except (ValueError, KeyError, TypeError, IndexError, RecursionError, configparser.Error):
        return "binary", {}
