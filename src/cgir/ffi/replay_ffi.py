"""Trace replay against a compiled candidate over the C ABI — the FFI core's
behavioral oracle for interpreted source languages (docs/design-ffi-pipeline.md
§6). Where the differential driver fuzzes two dylibs against each other, this
oracle replays *recorded real calls* (``replay.capture()``'s ``(args, result)``
pairs from the source language's own test run) against the candidate dylib and
requires agreement on every one.

The load-bearing conventions here were verified by experiment, not assumed:

- ``int`` crosses as ``i64`` with a mandatory range check — ctypes silently
  masks out-of-range ints (``c_int64(2**63) == -2**63``), so an unchecked
  trace could replay wrapped garbage and *falsely pass*. A violating value
  marks the whole function out of scope, not just the trace.
- ``str``/``bytes`` cross as ``(ptr, len)`` — never NUL-terminated ``CStr``,
  which silently truncates at embedded NULs.
- ``str``/``bytes`` *returns* cross as a by-value ``#[repr(C)]
  RustBuf{ptr,len,cap}`` freed through the candidate's exported
  ``cgir_buf_free`` (output size is unboundable from input size, which rules
  out caller-provided buffers).
- float equality is bitwise with all NaNs collapsed into one class — strict
  where semantics are observable (``0.0`` vs ``-0.0``), lenient where they
  aren't (NaN payloads).
- Panics are the harness's problem, not the prompt's: candidates are built
  with ``panic=abort``, replays run batched in a child process that announces
  each index *before* the FFI call, and an abort becomes a per-input rejection
  with that input as the counterexample — the parent respawns the child on the
  remaining tail so one panic doesn't discard the rest of the evidence.
"""

from __future__ import annotations

import json
import pickle
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from cgir.ffi.ir import Signature

Trace = tuple[tuple[Any, ...], Any]

_I64_MIN, _I64_MAX = -(2**63), 2**63 - 1
_PARAM_KINDS = ("i64", "f64", "bool", "str", "bytes")


def _kinds(sig: Signature) -> tuple[list[str], str]:
    """Flatten a Signature into the child protocol's kind strings."""
    kinds: list[str] = []
    for p in sig.params:
        if p.kind == "scalar" and p.scalar in ("i64", "f64", "bool"):
            kinds.append(p.scalar)
        elif p.kind == "slice":
            kinds.append("str" if p.text else "bytes")
        else:
            raise ValueError(f"unsupported param kind for replay: {p.kind}/{p.scalar}")
    ret = sig.ret
    if ret.startswith("buf:"):
        ret = ret.split(":", 1)[1]
    if ret not in _PARAM_KINDS:
        raise ValueError(f"unsupported return kind for replay: {sig.ret}")
    return kinds, ret


def _check_value(kind: str, v: Any) -> str:
    """Why ``v`` cannot soundly cross the FFI as ``kind`` ("" = it can).

    Type checks are exact (``type(v) is``): a bool recorded where an int is
    annotated, or an int where a float is annotated, is a trace/annotation
    disagreement we refuse to paper over in v1."""
    if kind == "i64":
        if type(v) is not int:
            return f"expected int, got {type(v).__name__}"
        if not (_I64_MIN <= v <= _I64_MAX):
            # ctypes would silently wrap this — a false-pass hazard, so the
            # function (which handled the value in Python) is out of scope.
            return f"int {v} exceeds i64 range"
    elif kind == "f64":
        if type(v) is not float:
            return f"expected float, got {type(v).__name__}"
    elif kind == "bool":
        if type(v) is not bool:
            return f"expected bool, got {type(v).__name__}"
    elif kind == "str":
        # accept str SUBCLASSES (markupsafe.Markup, test helpers): they encode
        # to identical UTF-8 bytes, and if a subclass changed behavior the Rust
        # would diverge and replay would catch it as a mismatch, not a false
        # pass. int/float/bool stay type-exact — there the coercion (bool-as-int,
        # int-as-float) is the actual false-pass hazard.
        if not isinstance(v, str):
            return f"expected str, got {type(v).__name__}"
        try:
            v.encode("utf-8")
        except UnicodeEncodeError:
            return "str is not UTF-8-encodable (lone surrogate)"
    elif kind == "bytes":
        if not isinstance(v, bytes | bytearray):
            return f"expected bytes, got {type(v).__name__}"
    else:
        return f"unsupported kind {kind!r}"
    return ""


def validate_traces(sig: Signature, traces: list[Trace]) -> str:
    """ "" when every recorded (args, result) can soundly cross the FFI;
    otherwise the reason. A violation marks the whole FUNCTION out of scope,
    not just the offending trace — the Python original demonstrably handles
    values the Rust version cannot even be handed."""
    if sig.ret == "void":
        return "traces unusable: void/None return is out of scope for v1"
    kinds, ret = _kinds(sig)
    for i, (args, expected) in enumerate(traces):
        if len(args) != len(kinds):
            return f"traces unusable: trace #{i} has {len(args)} args, signature has {len(kinds)}"
        for j, (kind, v) in enumerate(zip(kinds, args, strict=True)):
            err = _check_value(kind, v)
            if err:
                return f"traces unusable: trace #{i} arg {j}: {err}"
        err = _check_value(ret, expected)
        if err:
            return f"traces unusable: trace #{i} result: {err}"
    return ""


def _fmt_args(args: tuple[Any, ...]) -> str:
    s = ", ".join(repr(a) for a in args)
    return f"({s[:150]}...)" if len(s) > 150 else f"({s})"


# The generated child: stdlib-only, one JSON line per event on stdout. The
# {"calling": i} line is emitted and flushed BEFORE each FFI call so an abort
# (panic under panic=abort -> SIGABRT) is attributable to its input.
_CHILD_SOURCE = """\
import ctypes, json, math, pickle, struct, sys


class RustBuf(ctypes.Structure):
    _fields_ = [("ptr", ctypes.c_void_p), ("len", ctypes.c_size_t), ("cap", ctypes.c_size_t)]


_SCALAR = {"i64": ctypes.c_int64, "f64": ctypes.c_double, "bool": ctypes.c_bool}


def main():
    with open(sys.argv[1], "rb") as f:
        p = pickle.load(f)
    start = int(sys.argv[2])
    lib = ctypes.CDLL(p["dylib"])
    fn = getattr(lib, p["symbol"])
    argtypes = []
    for kind in p["param_kinds"]:
        if kind in ("str", "bytes"):
            argtypes += [ctypes.c_char_p, ctypes.c_size_t]
        else:
            argtypes.append(_SCALAR[kind])
    fn.argtypes = argtypes
    ret = p["ret"]
    free = None
    if p["ret_is_buf"]:
        fn.restype = RustBuf
        free = lib.cgir_buf_free
        free.argtypes = [RustBuf]
        free.restype = None
    else:
        fn.restype = _SCALAR[ret]

    for i in range(start, len(p["traces"])):
        args, expected = p["traces"][i]
        cargs = []
        for kind, v in zip(p["param_kinds"], args):
            if kind == "str":
                b = v.encode("utf-8")
                cargs += [b, len(b)]
            elif kind == "bytes":
                b = bytes(v)
                cargs += [b, len(b)]
            else:
                cargs.append(v)
        print(json.dumps({"calling": i}), flush=True)
        got = fn(*cargs)
        if p["ret_is_buf"]:
            data = ctypes.string_at(got.ptr, got.len) if got.len else b""
            free(got)
            exp = expected.encode("utf-8") if isinstance(expected, str) else bytes(expected)
            ok = data == exp
            detail = "" if ok else "expected %r got %r" % (exp, data)
        elif ret == "f64":
            ok = (math.isnan(got) and math.isnan(expected)) or struct.pack(
                "<d", got
            ) == struct.pack("<d", expected)
            detail = "" if ok else "expected %r got %r" % (expected, got)
        else:
            ok = got == expected
            detail = "" if ok else "expected %r got %r" % (expected, got)
        print(json.dumps({"i": i, "ok": ok, "detail": detail[:300]}), flush=True)
    print(json.dumps({"done": True}), flush=True)


main()
"""


def _consume(out: str, results: dict[int, tuple[bool, str]]) -> tuple[int | None, bool]:
    """Apply the child's event stream to ``results``; return (in-flight index
    with no verdict — the input a crash/timeout is attributed to — and whether
    the stream reached its normal end)."""
    inflight: int | None = None
    done = False
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "calling" in ev:
            inflight = int(ev["calling"])
        elif "i" in ev:
            results[int(ev["i"])] = (bool(ev["ok"]), str(ev.get("detail", "")))
            if inflight == int(ev["i"]):
                inflight = None
        elif ev.get("done"):
            done = True
    return inflight, done


def _run_batches(
    dylib: Path,
    symbol: str,
    kinds: list[str],
    ret: str,
    ret_is_buf: bool,
    traces: list[Trace],
    timeout: float,
    workdir: Path | None,
) -> tuple[dict[int, tuple[bool, str]], dict[int, str], str | None]:
    """Run the child over the trace list, respawning past crashes.

    Returns (per-index verdicts, {index: "crash …"/"timeout"}, harness error).
    A crash consumes only its own index; a timeout ends the run (there is no
    way to know the remaining traces wouldn't also hang)."""
    wd = workdir or Path(tempfile.mkdtemp(prefix="cgir-replay-"))
    runner = wd / "replay_child.py"
    runner.write_text(_CHILD_SOURCE)
    payload = wd / "payload.pkl"
    payload.write_bytes(
        pickle.dumps(
            {
                "dylib": str(dylib),
                "symbol": symbol,
                "param_kinds": kinds,
                "ret": ret,
                "ret_is_buf": ret_is_buf,
                "traces": traces,
            }
        )
    )
    results: dict[int, tuple[bool, str]] = {}
    hard: dict[int, str] = {}
    start = 0
    spawns = 0
    while start < len(traces):
        spawns += 1
        if spawns > len(traces) + 1:
            return results, hard, "respawn limit exceeded"
        proc = subprocess.Popen(
            [sys.executable, str(runner), str(payload), str(start)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            out, errout = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            inflight, _ = _consume(out, results)
            hard[inflight if inflight is not None else start] = "timeout"
            return results, hard, None
        inflight, done = _consume(out, results)
        if done and proc.returncode == 0:
            return results, hard, None
        if inflight is None:
            # died without an in-flight call: dylib/symbol/marshalling problem,
            # not a candidate-input crash.
            return results, hard, f"child failed before a call: {errout.strip()[-400:]}"
        rc = proc.returncode
        hard[inflight] = f"crash (signal {-rc})" if rc and rc < 0 else f"crash (exit {rc})"
        start = inflight + 1
    return results, hard, None


def replay_against_dylib(
    dylib: Path,
    symbol: str,
    sig: Signature,
    traces: list[Trace],
    *,
    timeout: float = 30.0,
    workdir: Path | None = None,
) -> str:
    """Replay every recorded (args, result) against ``symbol`` in ``dylib``.

    Returns "" when the candidate agrees on all traces, else a rejection
    reason in the same shape :func:`cgir.ffi.driver.differential` produces —
    the search loop treats them identically. The verified property is,
    explicitly: agreement on the recorded, non-raising inputs."""
    if not traces:
        return "replay: no captured traces to replay"
    err = validate_traces(sig, traces)
    if err:
        return err
    kinds, ret = _kinds(sig)
    results, hard, herr = _run_batches(
        dylib, symbol, kinds, ret, sig.ret.startswith("buf:"), traces, timeout, workdir
    )
    if herr:
        return f"replay: harness error — {herr}"
    for i, (args, _expected) in enumerate(traces):
        if i in hard:
            why = hard[i]
            extra = (
                " — candidate aborted (likely panic)"
                if why.startswith("crash")
                else " — candidate likely non-terminating"
            )
            return f"replay {why} on trace #{i}: {symbol}{_fmt_args(args)}{extra}"
        ok, detail = results.get(i, (False, "no verdict recorded"))
        if not ok:
            return f"replay mismatch on trace #{i}: {symbol}{_fmt_args(args)} {detail}"
    return ""
