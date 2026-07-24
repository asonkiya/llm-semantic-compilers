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
        "u8",
        "u16",
        "u32",
        "u64",
        "s8",
        "s16",
        "s32",
        "s64",
        "size_t",
        "bool",
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


def _line_start(text: str, idx: int) -> int:
    nl = text.rfind("\n", 0, idx)
    return nl + 1 if nl != -1 else 0


def _brace_depth(text: str, idx: int) -> int:
    """Net ``{`` nesting depth at ``idx``, skipping braces inside ``//`` and
    ``/* */`` comments and string/char literals — so a match inside a function
    body (a local variable, not a file-scope definition) is rejected."""
    depth = i = 0
    n = len(text)
    while i < idx and i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = n if nl == -1 else nl
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif c in "\"'":
            i += 1
            while i < n and text[i] != c:
                i += 2 if text[i] == "\\" else 1
            i += 1
        else:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
    return depth


def extract_definition(name: str, text: str) -> str | None:
    """The file-scope definition of ``name`` — a ``#define``, a
    ``name[...] = {...};`` table, a ``name = ...;`` const, or a
    ``name(...) {...}`` function — brace-matched, or None. Uses (``name[i]``,
    ``name()`` calls) are skipped: only a definition shape matches."""
    esc = re.escape(name)
    # #define name ...   (with line continuations)
    m = re.search(rf"^[ \t]*#[ \t]*define[ \t]+{esc}\b.*(?:\\\n.*)*", text, re.M)
    if m:
        return text[m.start() : m.end()]
    # array/table definition: `... name[...] = { ... };`
    for m in re.finditer(rf"(?<![\w.>]){esc}\s*\[[^\]]*\]\s*=\s*", text):
        if _brace_depth(text, m.start()) != 0:
            continue  # a local array inside a function body, not a file-scope table
        brace = text.find("{", m.end())
        semi = text.find(";", m.end())
        if brace != -1 and (semi == -1 or brace < semi):
            end = _match_braces(text, brace)
            end = text.find(";", end) + 1 if text[end : text.find(";", end)].strip() == "" else end
            return text[_line_start(text, m.start()) : end]
    # function definition: `... name(...) { ... }` (not a call/prototype)
    for m in re.finditer(rf"(?<![\w.>]){esc}\s*\([^;{{}}]*\)\s*\{{", text):
        if _brace_depth(text, m.start()) != 0:
            continue  # a call/definition nested in a body, not a file-scope function
        brace = text.index("{", m.end() - 1)
        return text[_line_start(text, m.start()) : _match_braces(text, brace)]
    # scalar const: `... name = ...;` at file scope (best-effort, single line)
    for m in re.finditer(rf"(?<![\w.>]){esc}\s*=\s*[^;{{}}]*;", text):
        if _brace_depth(text, m.start()) != 0:
            continue  # a local variable / assignment inside a function body
        head = text[_line_start(text, m.start()) : m.start()]
        if "(" not in head and "return" not in head:  # not an assignment inside a body
            return text[_line_start(text, m.start()) : m.end()]
    return None


def _referenced_idents(body: str) -> set[str]:
    return {w for w in re.findall(r"\b([A-Za-z_]\w*)\b", body) if w not in _KEYWORDS}


def lift_symbol(source: str, symbol: str, shim: str = "") -> tuple[str | None, list[str]]:
    """Assemble a standalone TU: ``shim`` + every in-file definition ``symbol``
    transitively references (tables, ``#define``s, consts, helper functions), in
    file order, ending with ``symbol``. Returns (tu_source | None if ``symbol``
    isn't found, sorted unresolved identifiers)."""
    target = extract_definition(symbol, source)
    if target is None:
        return None, []
    shim_names = set(re.findall(r"\b([A-Za-z_]\w*)\b", shim))
    pulled: dict[str, str] = {}  # name -> definition text
    unresolved: set[str] = set()
    queue = [symbol]
    defs_by_name = {symbol: target}
    while queue:
        name = queue.pop()
        if name in pulled:
            continue
        pulled[name] = defs_by_name[name]
        for ref in _referenced_idents(pulled[name]):
            if ref == name or ref in pulled or ref in shim_names:
                continue
            d = extract_definition(ref, source)
            if d is not None:
                defs_by_name[ref] = d
                queue.append(ref)
            else:
                unresolved.add(ref)
    # emit in file order (dependencies defined before use in the source), target last
    order = sorted(pulled, key=lambda n: source.find(pulled[n]))
    body = "\n\n".join(pulled[n] for n in order)
    tu = (shim + "\n\n" + body + "\n") if shim else body + "\n"
    return tu, sorted(unresolved - shim_names)
