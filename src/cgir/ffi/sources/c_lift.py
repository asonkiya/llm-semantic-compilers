"""Lift a C function + its file-scope dependencies out of a larger source file
into a standalone compilable translation unit.

The C->Rust pipeline needs a compilable TU (``--c-source``). A kernel function
often can't be lifted alone because it reads a file-scope *table* (the AES
S-box), a ``#define``, or a helper defined elsewhere in its ``.c``. This
tree-shakes exactly those — by name, with brace-matched extraction that's robust
to the macro noise (``____cacheline_aligned``, ``__alias(...)``) that defeats a
real C parse — so ``subw`` lifts together with ``aes_sbox`` and the pipeline can
rewrite it (``probe_context`` then injects the table's values into the prompt,
the model embeds them, the differential verifies).

    tu, unresolved = lift_symbol(aes_c_source, "subw", shim=KERNEL_SHIM)

``unresolved`` lists referenced identifiers with no in-file definition — they
must be covered by ``shim`` (types/macros) or the function isn't liftable.
"""

from __future__ import annotations

import re

# C keywords / builtins that are never file-scope definitions to pull in.
_KEYWORDS = frozenset(
    [
        "if",
        "else",
        "for",
        "while",
        "do",
        "switch",
        "case",
        "default",
        "break",
        "continue",
        "return",
        "goto",
        "sizeof",
        "int",
        "unsigned",
        "signed",
        "char",
        "short",
        "long",
        "double",
        "float",
        "void",
        "const",
        "static",
        "struct",
        "union",
        "enum",
        "typedef",
        "volatile",
        "register",
        "extern",
        "inline",
        "restrict",
        "_Bool",
        "asm",
        "__asm__",
        "NULL",
        "true",
        "false",
        # NOTE: u8/u32-style scalar spellings are deliberately NOT keywords:
        # an amalgamation may typedef them in-file (SQLite does), in which case
        # they must be pulled; when a shim provides them instead, shim_names
        # filtering skips them. size_t/bool stay — they come from headers.
        "size_t",
        "bool",
        # libc/libm — platform-provided, declared by the shim's headers. Never
        # pull an in-file override (SQLite's conditional `#define memcpy(D,S,N)`
        # conflicts with <string.h> and breaks the TU).
        "memcpy",
        "memmove",
        "memset",
        "memcmp",
        "strlen",
        "strcmp",
        "strncmp",
        "strcpy",
        "strncpy",
        "strchr",
        "strrchr",
        "strstr",
        "ceil",
        "floor",
        "fabs",
        "sqrt",
        "pow",
        "exp",
        "log",
        "sin",
        "cos",
        "tan",
        "atan2",
        "fmod",
        "isnan",
        "isinf",
        "malloc",
        "free",
        "realloc",
        "calloc",
        # preprocessor directive keywords / operators — never a referenced symbol
        "define",
        "defined",
        "undef",
        "include",
        "ifdef",
        "ifndef",
        "endif",
        "elif",
        "pragma",
        "error",
        "line",
    ]
)


def _match_braces(text: str, open_idx: int) -> int:
    """Index just past the ``}`` matching the ``{`` at ``open_idx``."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def _match_parens(text: str, open_idx: int) -> int:
    """Index just past the ``)`` matching the ``(`` at ``open_idx`` (or -1)."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _line_start(text: str, idx: int) -> int:
    nl = text.rfind("\n", 0, idx)
    return nl + 1 if nl != -1 else 0


def _mask_comments(text: str) -> str:
    """``text`` with ``//`` and ``/* */`` comments and string/char literals
    replaced by same-length runs of spaces (newlines kept). Offsets are
    preserved, so matches found in the mask slice correctly from the original —
    and a definition that only appears *inside a comment* (a commented-out
    function, common in real single-file libraries) never matches."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i)
            end = n if end == -1 else end
            for j in range(i, end):
                out[j] = " "
            i = end
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            for j in range(i, end):
                if out[j] != "\n":
                    out[j] = " "
            i = end
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            for k in range(i, min(j + 1, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _at_col0(text: str, idx: int) -> bool:
    """Whether the line containing ``idx`` begins at column 0 with a
    non-whitespace character that is not ``#`` — i.e. the definition's return
    type / qualifier is flush-left and the line is not a preprocessor directive
    (``#if defined(X)`` must never read as a definition of ``defined``). This is
    how real-world C writes *file-scope* definitions (SQLite, the kernel,
    tiny-AES all do); locals inside a function body are indented. It replaces a
    global brace-depth count, which drifts on a 270k-line amalgamation where a
    handful of literal braces escape the comment mask."""
    ls = _line_start(text, idx)
    return ls < len(text) and text[ls] not in " \t#"


def build_def_index(text: str, masked: str | None = None) -> dict[str, str]:
    """Map every file-scope definition name -> its source text, in ONE pass over
    the file. This is :func:`extract_definition` with the loop inverted: rather
    than scan the whole 9.5 MB source once per *queried* name (~0.65s each — and
    a single struct function pulls a closure of ~90 names, most of them struct
    fields/locals that resolve to nothing but still pay a full scan → tens of
    seconds, and a god-struct like ``Parse`` blows the closure into the
    thousands → a wedge), scan once and answer every lookup from the dict.

    Population order encodes the same precedence :func:`extract_definition` had
    by trying its patterns in sequence: object-macro > table > function > const
    > simple-typedef > struct-typedef > function-macro. First writer wins (a real
    function beats a ``#define name(args)`` alias for it — the optional-inline
    idiom, tiny-AES ``Multiply``)."""
    if masked is None:
        masked = _mask_comments(text)
    idx: dict[str, str] = {}

    def put(name: str, val: str) -> None:
        if name not in idx:
            idx[name] = val

    # 1. object-like macro: `#define name value` (name NOT followed by `(`).
    for m in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+(\w+)(?=[ \t]).*(?:\\\n.*)*", masked, re.M):
        put(m.group(1), text[m.start() : m.end()])
    # 2. array/table: `... name[...] = { ... };`
    for m in re.finditer(r"(?<![\w.>])(\w+)\s*\[[^\]]*\]\s*=\s*", masked):
        if not _at_col0(masked, m.start()):
            continue
        brace = masked.find("{", m.end())
        semi = masked.find(";", m.end())
        if brace != -1 and (semi == -1 or brace < semi):
            end = _match_braces(masked, brace)
            nsemi = masked.find(";", end)
            end = nsemi + 1 if masked[end:nsemi].strip() == "" else end
            put(m.group(1), text[_line_start(text, m.start()) : end])
    # 3. function definition: `name(params) {`, paren-balanced, `{` follows `)`.
    for m in re.finditer(r"(?<![\w.>])(\w+)\s*\(", masked):
        if m.group(1) in idx or not _at_col0(masked, m.start()):
            continue
        close = _match_parens(masked, masked.index("(", m.end() - 1))
        if close == -1:
            continue
        if re.match(r"\s*\{", masked[close:]):
            brace = masked.index("{", close)
            put(m.group(1), text[_line_start(text, m.start()) : _match_braces(masked, brace)])
    # 4. scalar const: `... name = ...;` at file scope (single line).
    for m in re.finditer(r"(?<![\w.>])(\w+)\s*=\s*[^;{}]*;", masked):
        if m.group(1) in idx or not _at_col0(masked, m.start()):
            continue
        head = masked[_line_start(masked, m.start()) : m.start()]
        if "(" not in head and "return" not in head:
            put(m.group(1), text[_line_start(text, m.start()) : m.end()])
    # 5. simple typedef: `typedef unsigned char u8;` / `typedef struct Foo Foo;`.
    for m in re.finditer(r"^typedef[^;{}\n]*\b(\w+)\s*(?:\[[^\]]*\])?\s*;", masked, re.M):
        put(m.group(1), text[m.start() : m.end()])
    # 6. struct/union/enum typedef with a body: `typedef struct {...} Foo;` — the
    # trailing names (`Foo`, `*PFoo`) all resolve to this definition.
    for m in re.finditer(r"^typedef\s+(?:struct|union|enum)\b[^{;]*\{", masked, re.M):
        end = _match_braces(masked, masked.index("{", m.start()))
        semi = masked.find(";", end)
        val = text[m.start() : semi + 1]
        for tn in re.findall(r"\b(\w+)\b", masked[end : semi + 1]):
            put(tn, val)
    # 7. function-like macro: `#define name(args) body` — lowest precedence, so a
    # real function of the same name (populated in pass 3) already won.
    for m in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+(\w+)\(.*(?:\\\n.*)*", masked, re.M):
        put(m.group(1), text[m.start() : m.end()])
    return idx


def extract_definition(
    name: str,
    text: str,
    masked: str | None = None,
    index: dict[str, str] | None = None,
) -> str | None:
    """The file-scope definition of ``name`` — a ``#define``, a
    ``name[...] = {...};`` table, a ``name = ...;`` const, a ``name(...) {...}``
    function, or a typedef — or None.

    Backed by :func:`build_def_index`: a lone call builds a one-shot index (same
    single-scan cost it always had); callers doing many lookups over the same
    source (``lift_symbols``, the sweep) build the index once and pass it in via
    ``index``, making each lookup O(1) instead of a fresh 9.5 MB scan."""
    if index is None:
        index = build_def_index(text, masked)
    return index.get(name)


def _referenced_idents(body: str) -> set[str]:
    """Identifiers referenced in ``body`` (a definition), ignoring C keywords and
    tokens that appear only in comments/strings — those aren't real dependencies."""
    code = _mask_comments(body)
    return {w for w in re.findall(r"\b([A-Za-z_]\w*)\b", code) if w not in _KEYWORDS}


# A reasonable default shim for kernel-/sqlite-flavored C: the scalar typedef
# spellings and annotation macros a lifted fragment is most likely to need but
# least likely to define at file scope (they usually live in headers). It
# deliberately defines NO functions — an unresolved call must fail the compile,
# not be papered over.
DEFAULT_SHIM = """\
#include <stdint.h>
#include <stddef.h>
#include <math.h>
#include <string.h>
typedef uint8_t u8; typedef int8_t i8;
typedef uint16_t u16; typedef int16_t i16;
typedef uint32_t u32; typedef int32_t i32;
typedef uint64_t u64; typedef int64_t i64;
#define SQLITE_PRIVATE
#define SQLITE_API
#define SQLITE_NOINLINE
#define ____cacheline_aligned
#define assert(x)
#define testcase(x)
#define ALWAYS(x) (x)
#define NEVER(x) (x)
"""


# A file-scope definition whose transitive in-file closure exceeds this many
# distinct definitions is treated as unliftable. In practice a leaf/helper lifts
# with a closure in the low tens; only functions reaching a god-struct (SQLite's
# `Parse`/`Vdbe`, whose struct graph pulls most of the schema) blow past it, and
# those aren't isolatable into a standalone TU anyway. The cap keeps lift O(cap),
# never O(whole symbol table). See tests/unit/test_c_lift.py::test_closure_cap.
MAX_CLOSURE = 400


def lift_symbols(
    source: str,
    symbols: list[str],
    shim: str = "",
    masked: str | None = None,
    cache: dict[str, str | None] | None = None,
    index: dict[str, str] | None = None,
) -> tuple[str | None, list[str], list[str]]:
    """Assemble one standalone TU covering every symbol in ``symbols``: ``shim``
    + the union of their transitive in-file definitions (tables, ``#define``s,
    consts, typedefs, helper functions), in file order. Returns
    (tu_source | None if *no* symbol was found, sorted unresolved identifiers,
    symbols that had no definition).

    ``masked`` and ``index`` (name -> definition text, from
    :func:`build_def_index`) let a sweep over many symbols of the *same* source
    pay the 9.5 MB masking and single indexing scan once instead of per lift.
    ``cache`` is accepted for backward compatibility and kept in sync. A closure
    exceeding :data:`MAX_CLOSURE` distinct defs aborts the lift (returns None)
    rather than running away over a god-struct's entire graph."""
    if masked is None:
        masked = _mask_comments(source)  # mask once; reused below
    if index is None:
        index = build_def_index(source, masked)  # one scan; every lookup O(1)
    if cache is None:
        cache = {}

    def _extract(name: str) -> str | None:
        if name not in cache:
            cache[name] = index.get(name)
        return cache[name]

    defs_by_name: dict[str, str] = {}
    missing: list[str] = []
    for sym in symbols:
        d = _extract(sym)
        if d is None:
            missing.append(sym)
        else:
            defs_by_name[sym] = d
    if not defs_by_name:
        return None, [], missing
    shim_names = set(re.findall(r"\b([A-Za-z_]\w*)\b", shim))
    pulled: dict[str, str] = {}  # name -> definition text
    unresolved: set[str] = set()
    queue = list(defs_by_name)
    while queue:
        name = queue.pop()
        if name in pulled:
            continue
        pulled[name] = defs_by_name[name]
        if len(pulled) > MAX_CLOSURE:  # runaway closure (god-struct) — not liftable
            return None, [], missing
        for ref in _referenced_idents(pulled[name]):
            if ref == name or ref in pulled or ref in shim_names:
                continue
            d = _extract(ref)
            if d is not None:
                defs_by_name[ref] = d
                queue.append(ref)
            else:
                unresolved.add(ref)
    # emit in file order (dependencies defined before use in the source)
    order = sorted(pulled, key=lambda n: source.find(pulled[n]))
    body = "\n\n".join(pulled[n] for n in order)
    tu = (shim + "\n\n" + body + "\n") if shim else body + "\n"
    return tu, sorted(unresolved - shim_names), missing


def lift_symbol(
    source: str,
    symbol: str,
    shim: str = "",
    masked: str | None = None,
    cache: dict[str, str | None] | None = None,
    index: dict[str, str] | None = None,
) -> tuple[str | None, list[str]]:
    """Single-symbol :func:`lift_symbols`. Returns (tu_source | None if
    ``symbol`` isn't found, sorted unresolved identifiers)."""
    tu, unresolved, _ = lift_symbols(source, [symbol], shim, masked, cache, index)
    return tu, unresolved
