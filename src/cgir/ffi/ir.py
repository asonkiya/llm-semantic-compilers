"""The FFI signature IR — the language-neutral vocabulary every rewrite pair
speaks (docs/design-ffi-pipeline.md §3).

A function's ABI is a list of ``(token, name)`` params plus a return token:

- scalar tokens — a C-spelling key of :data:`TYPE_MAP` (``"int"``, ``"u8"``,
  ``"double"``, canonical ``"i64"``/``"f64"``, …);
- ``"ptr:str:const|mut"`` / ``"ptr:buf:const|mut"`` — NUL-terminated string /
  byte-buffer pointers, fuzzable by the differential driver;
- ``"struct:Name:const|mut"`` — single-level struct pointer, gate-only.

:class:`FfiEntry` (historical name :class:`CEntry`) carries one worklist
function in this vocabulary. Source-language bindings produce entries; the
driver, targets, and gate consume them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# C type -> (rust type, ctypes name).
TYPE_MAP: dict[str, tuple[str, str]] = {
    "int": ("i32", "c_int"),
    "i32": ("i32", "c_int"),
    "unsigned": ("u32", "c_uint"),
    "unsigned int": ("u32", "c_uint"),
    "Bool": ("u32", "c_uint"),
    "u32": ("u32", "c_uint"),
    "double": ("f64", "c_double"),
    "float": ("f32", "c_float"),
    "char": ("i8", "c_byte"),
    "signed char": ("i8", "c_byte"),
    "i8": ("i8", "c_byte"),
    "unsigned char": ("u8", "c_ubyte"),
    "u8": ("u8", "c_ubyte"),
    "short": ("i16", "c_short"),
    "i16": ("i16", "c_short"),
    "LogEst": ("i16", "c_short"),
    "u16": ("u16", "c_ushort"),
    "long": ("i64", "c_longlong"),
    "long long": ("i64", "c_longlong"),
    "i64": ("i64", "c_longlong"),
    "sqlite3_int64": ("i64", "c_longlong"),
    "sqlite_int64": ("i64", "c_longlong"),
    "unsigned long": ("u64", "c_ulonglong"),
    "unsigned long long": ("u64", "c_ulonglong"),
    "u64": ("u64", "c_ulonglong"),
    "sqlite3_uint64": ("u64", "c_ulonglong"),
    "sqlite_uint64": ("u64", "c_ulonglong"),
    "tRowcnt": ("u64", "c_ulonglong"),
}
# ctypes-name -> (C concrete type, width bits, is_signed, is_float)
_C_INFO: dict[str, tuple[str, int, int, int]] = {
    "c_int": ("int32_t", 32, 1, 0),
    "c_uint": ("uint32_t", 32, 0, 0),
    "c_short": ("int16_t", 16, 1, 0),
    "c_ushort": ("uint16_t", 16, 0, 0),
    "c_byte": ("int8_t", 8, 1, 0),
    "c_ubyte": ("uint8_t", 8, 0, 0),
    "c_longlong": ("int64_t", 64, 1, 0),
    "c_ulonglong": ("uint64_t", 64, 0, 0),
    "c_double": ("double", 64, 1, 1),
    "c_float": ("float", 32, 1, 1),
}


@dataclass(frozen=True)
class ScalarType:
    """One canonical scalar in the FFI vocabulary — the forward-looking face
    of :data:`_C_INFO` (bindings map their language's spellings onto these).
    """

    name: str  # canonical: "i8".."i64", "u8".."u64", "f32", "f64"
    ctypes_name: str
    c_type: str
    bits: int
    signed: bool
    is_float: bool


# Canonical scalars, derived from _C_INFO so there is one source of truth.
_CANONICAL = {
    "c_byte": "i8",
    "c_short": "i16",
    "c_int": "i32",
    "c_longlong": "i64",
    "c_ubyte": "u8",
    "c_ushort": "u16",
    "c_uint": "u32",
    "c_ulonglong": "u64",
    "c_float": "f32",
    "c_double": "f64",
}
SCALARS: dict[str, ScalarType] = {
    canon: ScalarType(
        name=canon,
        ctypes_name=cname,
        c_type=_C_INFO[cname][0],
        bits=_C_INFO[cname][1],
        signed=bool(_C_INFO[cname][2]),
        is_float=bool(_C_INFO[cname][3]),
    )
    for cname, canon in _CANONICAL.items()
}


@dataclass(frozen=True)
class Param:
    """One FFI-IR parameter (docs/design-ffi-pipeline.md §3).

    ``kind`` for the Python pair: ``"scalar"`` (``scalar`` names ``"i64"`` /
    ``"f64"`` / ``"bool"`` — ``bool`` is a Python-pair scalar, deliberately not
    in the C fuzz registry) or ``"slice"`` (a ``(ptr, len)`` pair; ``text``
    distinguishes str/UTF-8 from bytes). The C pair still speaks its string
    tokens (``"int"``, ``"ptr:str:const"``, …); unifying it onto these
    dataclasses is a later cleanup, not behavior.
    """

    name: str
    kind: str  # "scalar" | "slice"
    scalar: str | None = None  # canonical scalar name when kind == "scalar"
    text: bool = False  # kind == "slice": True = str (UTF-8), False = bytes
    mutable: bool = False
    # A pure method that only reads scalar/str/bytes fields of ``self`` is a pure
    # function of those fields. Such params are marked ``from_self`` and carry the
    # field name in ``name`` — the wrapper reads ``self.<name>`` and replay
    # expands the captured ``self`` the same way. The Rust boundary sees a plain
    # param either way.
    from_self: bool = False


@dataclass(frozen=True)
class Signature:
    """A function's FFI-IR signature. ``ret`` is ``"void"``, a canonical
    scalar name, or ``"buf:str"`` / ``"buf:bytes"`` (a Rust-allocated
    ``RustBuf{ptr,len,cap}`` return, freed via ``cgir_buf_free``).

    ``self_param`` is the receiver name (``"self"``/``"cls"``) for a value-self
    method, else ``None`` — it tells the wrapper to emit ``def f(self, ...)`` and
    read the ``from_self`` params off ``self``."""

    params: tuple[Param, ...]
    ret: str
    self_param: str | None = None


@dataclass
class CEntry:
    component_id: str
    name: str
    ret: str
    params: list[tuple[str, str]]
    source: str
    # in-repo callees also in the worklist — the non-leaf edges. A rewritten
    # Rust caller reaches them as `extern "C"` symbols: the original C during
    # verification (the oracle exports every de-static'd worklist symbol), the
    # rewritten Rust after link-back.
    callees: list[str] = field(default_factory=list)
    # {struct name: C definition} for struct-pointer params — the model mirrors
    # these as #[repr(C)]. Non-empty marks a gate-only function (the isolated
    # differential can't build a valid instance to fuzz).
    struct_defs: dict[str, str] = field(default_factory=dict)

    @property
    def gate_only(self) -> bool:
        return bool(self.struct_defs)


# Forward-looking name; CEntry retained everywhere for compatibility.
FfiEntry = CEntry


def _toposort(entries: dict[str, CEntry]) -> list[CEntry]:
    """Callees before callers; ties by id. Cycles fall back to id order."""
    name_to_id = {e.name: cid for cid, e in entries.items()}
    order: list[CEntry] = []
    seen: set[str] = set()
    temp: set[str] = set()

    def visit(cid: str) -> None:
        if cid in seen or cid not in entries:
            return
        if cid in temp:  # cycle — leave for id-order fallback
            return
        temp.add(cid)
        for callee_name in entries[cid].callees:
            cbid = name_to_id.get(callee_name)
            if cbid is not None:
                visit(cbid)
        temp.discard(cid)
        seen.add(cid)
        order.append(entries[cid])

    for cid in sorted(entries):
        visit(cid)
    return order
