"""Exercises each eligible function with several inputs: the CALLS edges from
these tests populate ``covered_by`` (so ``covered:true`` matches), and the
calls are what ``replay.capture()`` records as traces.
"""

from __future__ import annotations

from mathlib import Rect, clamp, fnv1a, greet, noop, pick, scale, shout


def test_rect_methods() -> None:
    assert Rect(3, 4).area() == 12
    assert Rect(0, 9).area() == 0
    assert Rect(5, 5).area() == 25
    assert Rect(2, 3).scaled_area(4) == 24
    assert Rect(1, 1).scaled_area(10) == 10
    assert Rect(6, 7).scaled_area(0) == 0


def test_clamp() -> None:
    assert clamp(5, 0, 10) == 5
    assert clamp(-3, 0, 10) == 0
    assert clamp(42, 0, 10) == 10
    assert clamp(7, 7, 7) == 7


def test_fnv1a() -> None:
    assert fnv1a(b"") == 0x811C9DC5
    assert fnv1a(b"a") != fnv1a(b"b")
    assert fnv1a(b"hello") == fnv1a(b"hello")
    assert isinstance(fnv1a(b"\x00\xff\x10"), int)


def test_shout() -> None:
    assert shout("hi") == "HI"
    assert shout("") == ""
    assert shout("straße") == "STRASSE"


def test_scale() -> None:
    assert scale(2.0, 3.0) == 6.0
    assert scale(-1.5, 4.0) == -6.0
    assert scale(0.0, 99.0) == 0.0


def test_ineligible_are_still_covered() -> None:
    # these are covered (so they reach the worklist) but ineligible by ABI —
    # the worklist must route them to `excluded`, not rewrite them.
    assert pick([1, 2, 3]) == 1
    assert greet("x") == "hi x"
    assert noop(5) is None
