"""Pure, fully-typed leaf functions — the Python->Rust worklist fixture.

The first four are eligible (params + return in int|float|bool|str|bytes); the
last three each trip one exclusion rule (container / missing annotation / void
return) and must land in the worklist's ``excluded`` list.
"""

from __future__ import annotations


def clamp(x: int, lo: int, hi: int) -> int:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def fnv1a(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def shout(s: str) -> str:
    return s.upper()


def scale(x: float, k: float) -> float:
    return x * k


# --- ineligible (exclusion-test) shapes -------------------------------------


def pick(xs: list[int]) -> int:  # container param
    return xs[0] if xs else 0


def greet(name) -> str:  # missing annotation  # type: ignore[no-untyped-def]
    return "hi " + str(name)


def noop(x: int) -> None:  # void return
    _ = x
