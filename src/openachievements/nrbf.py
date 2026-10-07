""".NET BinaryFormatter saves (MS-NRBF), read into plain values.

Many Unity games save a C# object with BinaryFormatter: a header, then
records for classes, strings, arrays and primitive values, with references
between them. This walks those records as bytes and builds dicts, lists and
scalars from them; it never runs .NET deserialisation and never creates an
object a file names, so a save cannot make it do anything but read.

Shapes it tidies: a property's backing field `<Level>k__BackingField` is
`Level`; a `List<T>` is its first `_size` items; a `Dictionary<K,V>` with
simple keys is a dict. So the dotted `field` rules for JSON saves apply.

Bounded: a malformed or hostile file raises NrbfError; record counts, array
lengths and nesting are checked against what the file can hold.
"""
from __future__ import annotations

import struct

MAX_RECORDS = 2_000_000
MAX_DEPTH = 64
_PRIM = {1: ("<?", 1), 2: ("<B", 1), 6: ("<d", 8), 7: ("<h", 2), 8: ("<i", 4), 9: ("<q", 8), 10: ("<b", 1),
         11: ("<f", 4), 12: ("<q", 8), 13: ("<q", 8), 14: ("<H", 2), 15: ("<I", 4), 16: ("<Q", 8)}


class NrbfError(ValueError):
    pass


class _Ref:
    __slots__ = ("id",)

    def __init__(self, ref: int):
        self.id = ref


class _Class:
    __slots__ = ("name", "members", "values")

    def __init__(self, name: str, members: list[str]):
        self.name, self.members, self.values = name, members, []


def is_nrbf(data: bytes) -> bool:
    """A BinaryFormatter header: record 0, a root id, header id -1, version 1.0."""
    return len(data) > 17 and data[:1] == b"\x00" and data[5:9] == b"\xff\xff\xff\xff" \
        and data[9:17] == b"\x01\x00\x00\x00\x00\x00\x00\x00"


class _Reader:
    def __init__(self, data: bytes):
        self.d, self.i = data, 0
        self.objects: dict[int, object] = {}
        self.meta: dict[int, tuple] = {}      # object id of a class record -> (name, members, types, extras)
        self.records = 0

    def need(self, n: int) -> None:
        if n < 0 or self.i + n > len(self.d):
            raise NrbfError("record runs past the end of the file")

    def unpack(self, fmt: str, size: int):
        self.need(size)
        value = struct.unpack_from(fmt, self.d, self.i)[0]
        self.i += size
        return value

    def byte(self) -> int:
        return self.unpack("<B", 1)

    def i32(self) -> int:
        return self.unpack("<i", 4)

    def string(self) -> str:
        n, shift = 0, 0
        for _ in range(5):
            b = self.byte()
            n |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        else:
            raise NrbfError("string length is malformed")
        self.need(n)
        text = self.d[self.i:self.i + n].decode("utf-8", "replace")
        self.i += n
        return text

    def count(self, n: int, each: int = 1) -> int:
        if n < 0 or n * each > len(self.d) - self.i + 1:
            raise NrbfError("a count larger than the file")
        return n

    def primitive(self, kind: int):
        if kind in _PRIM:
            fmt, size = _PRIM[kind]
            return self.unpack(fmt, size)
        if kind in (5, 18):                   # Decimal is written as text, String too
            return self.string()
        if kind == 3:                         # Char: one UTF-8 character
            first = self.d[self.i] if self.i < len(self.d) else 0
            size = 1 if first < 0x80 else 2 if first < 0xE0 else 3 if first < 0xF0 else 4
            self.need(size)
            ch = self.d[self.i:self.i + size].decode("utf-8", "replace")
            self.i += size
            return ch
        if kind == 17:
            return None
        raise NrbfError(f"unknown primitive type {kind}")

    # ---- records ----

    def member_types(self, count: int) -> tuple[list[int], list]:
        types = [self.byte() for _ in range(count)]
        extras = []
        for t in types:
            if t in (0, 7):
                extras.append(self.byte())
            elif t == 3:
                extras.append(self.string())
            elif t == 4:
                extras.append((self.string(), self.i32()))
            elif t in (1, 2, 5, 6):
                extras.append(None)
            else:
                raise NrbfError(f"unknown member type {t}")
        return types, extras

    def class_info(self) -> tuple[int, str, list[str]]:
        oid, name = self.i32(), self.string()
        n = self.count(self.i32())
        return oid, name, [self.string() for _ in range(n)]

    def class_values(self, oid: int, name: str, members: list[str], types, extras) -> _Class:
        obj = _Class(name, members)
        self.objects[oid] = obj
        for k in range(len(members)):
            if types is not None and types[k] == 0:
                obj.values.append(self.primitive(extras[k]))
            else:
                obj.values.append(self.record())
        return obj

    def record(self):
        self.records += 1
        if self.records > MAX_RECORDS:
            raise NrbfError("too many records")
        kind = self.byte()
        if kind == 0:                                         # stream header
            self.need(16)
            self.i += 16
            return self.record()
        if kind == 1:                                         # class like an earlier one
            oid, meta_id = self.i32(), self.i32()
            if meta_id not in self.meta:
                raise NrbfError("a class refers to one not seen")
            name, members, types, extras = self.meta[meta_id]
            self.meta[oid] = self.meta[meta_id]
            return self.class_values(oid, name, members, types, extras)
        if kind in (2, 3):                                    # members without types
            oid, name, members = self.class_info()
            if kind == 3:
                self.i32()
            self.meta[oid] = (name, members, None, None)
            return self.class_values(oid, name, members, None, None)
        if kind in (4, 5):                                    # members with types
            oid, name, members = self.class_info()
            types, extras = self.member_types(len(members))
            if kind == 5:
                self.i32()
            self.meta[oid] = (name, members, types, extras)
            return self.class_values(oid, name, members, types, extras)
        if kind == 6:
            oid = self.i32()
            self.objects[oid] = value = self.string()
            return value
        if kind == 7:                                         # BinaryArray
            oid, shape, rank = self.i32(), self.byte(), self.count(self.i32(), 4)
            lengths = [self.count(self.i32()) for _ in range(rank)]
            if shape in (3, 4, 5):
                for _ in range(rank):
                    self.i32()
            (item_type,), (extra,) = self.member_types(1)
            total = 1
            for n in lengths:
                total *= n
            items = self.items(self.count(total), item_type == 0, extra)
            self.objects[oid] = items
            return items
        if kind == 8:
            return self.primitive(self.byte())
        if kind == 9:
            return _Ref(self.i32())
        if kind == 10:
            return None
        if kind == 11:
            return _END
        if kind == 12:
            self.i32()
            self.string()
            return self.record()                              # a library name: the next record is the value
        if kind == 13:
            return _Nulls(self.byte())
        if kind == 14:
            return _Nulls(self.count(self.i32()))
        if kind == 15:
            oid, n, prim = self.i32(), self.count(self.i32()), self.byte()
            if prim == 2:                                     # a byte array: kept short, it is data not values
                self.need(n)
                self.i += n
                self.objects[oid] = value = f"<{n} bytes>"
                return value
            items = [self.primitive(prim) for _ in range(n)]
            self.objects[oid] = items
            return items
        if kind in (16, 17):
            oid, n = self.i32(), self.count(self.i32())
            items = self.items(n, False, None)
            self.objects[oid] = items
            return items
        raise NrbfError(f"unknown record type {kind}")

    def items(self, n: int, primitive: bool, prim_type) -> list:
        out: list = []
        while len(out) < n:
            if primitive:
                out.append(self.primitive(prim_type))
                continue
            value = self.record()
            if isinstance(value, _Nulls):
                out.extend([None] * min(value.n, n - len(out)))
            else:
                out.append(value)
        return out


class _Nulls:
    __slots__ = ("n",)

    def __init__(self, n: int):
        self.n = n


_END = object()


def _clean(name: str) -> str:
    if name.startswith("<") and ">k__BackingField" in name:
        return name[1:name.index(">")]
    return name


def _plain(value, objects: dict, depth: int = 0, seen: frozenset = frozenset()):
    if depth > MAX_DEPTH:
        return None
    if isinstance(value, _Ref):
        if value.id in seen or value.id not in objects:
            return None
        return _plain(objects[value.id], objects, depth + 1, seen | {value.id})
    if isinstance(value, list):
        return [_plain(v, objects, depth + 1, seen) for v in value]
    if isinstance(value, _Class):
        raw = {_clean(m): v for m, v in zip(value.members, value.values)}
        if value.name.startswith("System.Collections.Generic.List`1") and "_items" in raw:
            items = _plain(raw["_items"], objects, depth + 1, seen) or []
            size = raw.get("_size")
            return items[:size] if isinstance(size, int) else items
        if value.name.startswith("System.Collections.Generic.Dictionary`2") and "KeyValuePairs" in raw:
            pairs = _plain(raw["KeyValuePairs"], objects, depth + 1, seen) or []
            if all(isinstance(p, dict) and isinstance(p.get("key"), (str, int, float, bool)) for p in pairs):
                return {str(p["key"]): p.get("value") for p in pairs}
            return pairs
        return {k: _plain(v, objects, depth + 1, seen) for k, v in raw.items()}
    if isinstance(value, _Nulls):
        return None
    return value


def parse(data: bytes) -> object:
    """The root object of a BinaryFormatter stream, as plain values."""
    if data[:1] != b"\x00" or len(data) < 17:
        raise NrbfError("not a BinaryFormatter stream")
    reader = _Reader(data)
    reader.byte()
    root_id = reader.i32()
    reader.need(12)
    reader.i += 12
    while reader.i < len(data):
        if reader.record() is _END:
            break
    if root_id not in reader.objects:
        raise NrbfError("the root object is missing")
    return _plain(_Ref(root_id), reader.objects)
