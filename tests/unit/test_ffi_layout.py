"""M1 of the FFI-core extraction (docs/design-ffi-pipeline.md §5): the
assembled pair module keeps its historical public surface, and the moved
machinery is the SAME objects (not copies) — so monkeypatching either module
affects both, and downstream importers (CLI, benchmarks) are unaffected.
"""

from __future__ import annotations

import cgir.rewrite_c_rust as rcr
from cgir.ffi import driver, gate, ir
from cgir.ffi.sources import c as c_source
from cgir.ffi.targets import rust as rust_target

# every name the CLI, benchmarks, and tests have historically imported
_PUBLIC_SURFACE = [
    "CEntry",
    "_assemble_winner_bodies",
    "_build_rust_staticlib",
    "_driver_source",
    "_patch_source",
    "build_c_rust_prompt",
    "c_rust_worklist",
    "compile_oracle",
    "contract_check",
    "differential",
    "exported_symbols",
    "extern_block",
    "link_back",
    "probe_context",
    "run_c_rust",
    "rust_signature",
    "suspect_global_reads",
    "try_rustc",
    "whole_program_gate",
]


def test_historical_surface_importable() -> None:
    for name in _PUBLIC_SURFACE:
        assert hasattr(rcr, name), f"cgir.rewrite_c_rust.{name} missing"


def test_reexports_are_the_same_objects() -> None:
    # identity, not equality: a copy would silently break monkeypatching and
    # split behavior between the pair module and the core.
    assert rcr.CEntry is ir.CEntry and rcr.FfiEntry is ir.CEntry
    assert rcr.differential is driver.differential
    assert rcr._driver_source is driver._driver_source
    assert rcr.whole_program_gate is gate.whole_program_gate
    assert rcr.c_rust_worklist is c_source.c_rust_worklist
    assert rcr.link_back is c_source.link_back
    assert rcr.try_rustc is rust_target.try_rustc
    assert rcr._build_rust_staticlib is rust_target._build_rust_staticlib


def test_scalar_registry_agrees_with_c_info() -> None:
    # SCALARS is derived from _C_INFO — one source of truth for widths/signs.
    for canon, s in ir.SCALARS.items():
        c_type, bits, signed, is_float = ir._C_INFO[s.ctypes_name]
        assert (s.c_type, s.bits, s.signed, s.is_float) == (
            c_type,
            bits,
            bool(signed),
            bool(is_float),
        ), canon
    assert set(ir.SCALARS) == {"i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64", "f32", "f64"}
