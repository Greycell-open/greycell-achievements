"""Unreal Engine save games (`.sav`, "GVAS"), read into plain values.

Most Unreal games save a `USaveGame` object with `UGameplayStatics`: a small
header, then the object's properties one after another. This reads the
properties a save rule can use (numbers, text, booleans, and arrays and maps
of those, and structs made of them) into dicts and lists, so the same dotted
`field` rules as for JSON saves apply:

    SavedIntMap.Total_NumItemsCrafted

Anything it does not understand (object references, custom binary
serialisation) is skipped by its recorded size, never guessed at. Both
property-tag layouts are read: the one before Unreal Engine 5.4 and the
"complete type name" one after it.

Read-only and bounded: a malformed file raises GvasError, never loops or
allocates without limit.
"""
from __future__ import annotations

import struct

MAX_DEPTH = 16
UE5_COMPLETE_TYPE_NAME = 1012       # FPackageFileVersionUE5 PROPERTY_TAG_COMPLETE_TYPE_NAME

_SCALARS = {
    "IntProperty": ("<i", 4), "Int64Property": ("<q", 8), "Int16Property": ("<h", 2), "Int8Property": ("<b", 1),
    "UInt32Property": ("<I", 4), "UInt64Property": ("<Q", 8), "UInt16Property": ("<H", 2),
    "FloatProperty": ("<f", 4), "DoubleProperty": ("<d", 8),
}
_TEXTS = ("StrProperty", "NameProperty", "EnumProperty", "SoftObjectProperty", "ObjectProperty")


class GvasError(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes, start: int = 0, end: int | None = None):
        self.data, self.i, self.end = data, start, len(data) if end is None else end

    def take(self, n: int) -> bytes:
        if n < 0 or self.i + n > self.end:
            raise GvasError("unexpected end of save")
        chunk = self.data[self.i:self.i + n]
        self.i += n
        return chunk

    def unpack(self, fmt: str, size: int):
        return struct.unpack(fmt, self.take(size))[0]

    def i32(self) -> int:
        return self.unpack("<i", 4)

    def u8(self) -> int:
        return self.take(1)[0]

    def fstring(self) -> str:
        n = self.i32()
        if n == 0:
            return ""
        if n < 0:
            return self.take(-2 * n)[:-2].decode("utf-16-le", "replace")
        return self.take(n)[:-1].decode("utf-8", "replace")


def _header(r: _Reader) -> int:
    """Skips the header; returns the UE5 package version (0 before UE5)."""
    if r.take(4) != b"GVAS":
        raise GvasError("not an Unreal save (no GVAS header)")
    save_version = r.i32()
    r.i32()                                           # UE4 package version
    ue5 = r.i32() if save_version >= 3 else 0
    r.take(6); r.i32(); r.fstring()                   # engine version: major, minor, patch, changelist, branch
    if save_version >= 2:
        r.i32()                                       # custom version format
        count = r.i32()
        if not 0 <= count <= 10000:
            raise GvasError("implausible custom version count")
        r.take(20 * count)
    r.fstring()                                       # the save game class
    return ue5


def _type_name(r: _Reader, depth: int = 0) -> tuple[str, list]:
    if depth > MAX_DEPTH:
        raise GvasError("type name nested too deeply")
    name = r.fstring()
    count = r.i32()
    if not 0 <= count <= 8:
        raise GvasError("implausible type parameters")
    return name, [_type_name(r, depth + 1) for _ in range(count)]


def _value(r: _Reader, kind: tuple[str, list], depth: int, new_layout: bool = True):
    name, params = kind
    if name in _SCALARS:
        return r.unpack(*_SCALARS[name])
    if name in _TEXTS:
        return r.fstring()
    if name == "BoolProperty":
        return bool(r.u8())
    if name == "ByteProperty":
        return r.fstring() if params and params[0][0] not in ("None", "") else r.u8()
    if name == "StructProperty":
        return _properties(r, r.end, depth + 1, new_layout=new_layout)
    raise GvasError(f"cannot read a {name} here")


def _container(r: _Reader, kind: tuple[str, list], end: int, depth: int, new_layout: bool = True):
    name, params = kind
    if name == "ArrayProperty" or name == "SetProperty":
        if name == "SetProperty":
            r.i32()                                   # elements to remove
        count = r.i32()
        if not 0 <= count <= end - r.i:
            raise GvasError("implausible element count")
        return [_value(r, params[0], depth, new_layout) for _ in range(count)]
    if name == "MapProperty":
        r.i32()                                       # keys to remove
        count = r.i32()
        if not 0 <= count <= end - r.i:
            raise GvasError("implausible map size")
        out = {}
        for _ in range(count):
            key = _value(r, params[0], depth, new_layout)
            out[str(key)] = _value(r, params[1], depth, new_layout)
        return out
    return _value(r, kind, depth, new_layout)


def _old_tag(r: _Reader) -> tuple[tuple[str, list], int, bool | None]:
    """Before UE 5.4: type name, int64 size, then type-specific extras."""
    kind = r.fstring()
    size = r.unpack("<q", 8)
    bool_value = None
    params: list = []
    if kind == "StructProperty":
        params = [(r.fstring(), [])]
        r.take(16)
    elif kind == "BoolProperty":
        bool_value = bool(r.u8())
    elif kind in ("ByteProperty", "EnumProperty"):
        params = [(r.fstring(), [])]
    elif kind in ("ArrayProperty", "SetProperty"):
        params = [(r.fstring(), [])]
    elif kind == "MapProperty":
        params = [(r.fstring(), []), (r.fstring(), [])]
    if r.u8():
        r.take(16)                                    # property guid
    return (kind, params), size, bool_value


def _new_tag(r: _Reader) -> tuple[tuple[str, list], int, bool | None]:
    """UE 5.4 and later: the complete type name, int32 size, then flags."""
    kind = _type_name(r)
    size = r.i32()
    flags = r.u8()
    if flags & 0x01:
        r.i32()                                       # array index
    if flags & 0x02:
        r.take(16)                                    # property guid
    if flags & 0x04:
        if r.u8() & 0x02:                             # overridable serialisation information
            r.take(2)
    bool_value = bool(flags & 0x10) if kind[0] == "BoolProperty" else None
    return kind, size, bool_value


def _properties(r: _Reader, end: int, depth: int, *, new_layout: bool) -> dict:
    if depth > MAX_DEPTH:
        raise GvasError("properties nested too deeply")
    out: dict = {}
    while r.i < end:
        name = r.fstring()
        if name in ("None", ""):
            break
        kind, size, bool_value = _new_tag(r) if new_layout else _old_tag(r)
        if size < 0 or r.i + size > end:
            raise GvasError(f"property {name!r} runs past the end of the save")
        start = r.i
        if bool_value is not None:
            out[name] = bool_value
        else:
            try:
                inner = _Reader(r.data, start, start + size)
                if kind[0] == "StructProperty":
                    out[name] = _properties(inner, inner.end, depth + 1, new_layout=new_layout)
                else:
                    out[name] = _container(inner, kind, inner.end, depth, new_layout)
            except (GvasError, struct.error, UnicodeDecodeError):
                pass                                  # not something rules read: skipped by its size
        r.i = start + size
    return out


def _peek_i32(data: bytes, at: int) -> int:
    return struct.unpack_from("<i", data, at)[0] if at + 4 <= len(data) else 0


def parse(data: bytes) -> dict:
    """A save's top-level properties as plain values."""
    r = _Reader(data)
    try:
        ue5 = _header(r)
        new_layout = ue5 >= UE5_COMPLETE_TYPE_NAME
        # Some engine versions write one zero byte after the class name; the
        # first property name's length says which (names are short).
        if len(data) >= r.i + 5 and data[r.i] == 0 and not 0 < _peek_i32(data, r.i) <= 1024 \
                and 0 < _peek_i32(data, r.i + 1) <= 1024:
            r.i += 1
        return _properties(r, len(data), 0, new_layout=new_layout)
    except struct.error as exc:
        raise GvasError(f"malformed save ({exc})") from None
