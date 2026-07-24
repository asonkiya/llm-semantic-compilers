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
        # preprocessor directive keywords — never a referenced symbol
        "define",
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


def _brace_depth(masked: str, idx: int) -> int:
    """Net ``{`` nesting depth at ``idx`` in comment/string-masked text — so a
    match inside a function body (a local variable, not a file-scope definition)
    is rejected."""
    return masked.count("{", 0, idx) - masked.count("}", 0, idx)


def extract_definition(name: str, text: str) -> str | None:
    """The file-scope definition of ``name`` — a ``#define``, a
    ``name[...] = {...};`` table, a ``name = ...;`` const, or a
    ``name(...) {...}`` function — brace-matched, or None. Matching runs against
    a comment/string-masked copy (so uses, and code inside comments, are never
    mistaken for a definition), but the returned text is sliced from the
    original."""
    esc = re.escape(name)
    masked = _mask_comments(text)
    # object-like macro: `#define name value` (a constant; name NOT immediately
    # followed by `(`). Function-like macros are handled LAST, below, so a real
    # function wins over a `#define name(args) ...` alias for it (the common
    # optional-inline idiom, e.g. tiny-AES `Multiply`/`getSBoxValue`).
    m = re.search(rf"^[ \t]*#[ \t]*define[ \t]+{esc}(?=[ \t]).*(?:\\\n.*)*", masked, re.M)
    if m:
        return text[m.start() : m.end()]
    # array/table definition: `... name[...] = { ... };`
    for m in re.finditer(rf"(?<![\w.>]){esc}\s*\[[^\]]*\]\s*=\s*", masked):
        if _brace_depth(masked, m.start()) != 0:
            continue  # a local array inside a function body, not a file-scope table
        brace = masked.find("{", m.end())
        semi = masked.find(";", m.end())
        if brace != -1 and (semi == -1 or brace < semi):
            end = _match_braces(masked, brace)
            nsemi = masked.find(";", end)
            end = nsemi + 1 if masked[end:nsemi].strip() == "" else end
            return text[_line_start(text, m.start()) : end]
    # function definition: `name(params) {` where the `{` follows the *matched*
    # close paren directly (only whitespace between). Paren-balanced — so a macro
    # use like `getSBoxInvert(num)` doesn't run its param scan on into a later
    # function's `(...) {`.
    for m in re.finditer(rf"(?<![\w.>]){esc}\s*\(", masked):
        if _brace_depth(masked, m.start()) != 0:
            continue  # a call nested in a body, not a file-scope definition
        close = _match_parens(masked, masked.index("(", m.end() - 1))
        if close == -1:
            continue
        rest = masked[close:]
        if re.match(r"\s*\{", rest):
            brace = masked.index("{", close)
            return text[_line_start(text, m.start()) : _match_braces(masked, brace)]
    # scalar const: `... name = ...;` at file scope (best-effort, single line)
    for m in re.finditer(rf"(?<![\w.>]){esc}\s*=\s*[^;{{}}]*;", masked):
        if _brace_depth(masked, m.start()) != 0:
            continue  # a local variable / assignment inside a function body
        head = masked[_line_start(masked, m.start()) : m.start()]
        if "(" not in head and "return" not in head:  # not an assignment inside a body
            return text[_line_start(text, m.start()) : m.end()]
    # function-like macro: `#define name(args) body` — only reached if no real
    # function of this name exists (it's then the active definition, e.g. a
    # commented-out function beside its macro).
    m = re.search(rf"^[ \t]*#[ \t]*define[ \t]+{esc}\(.*(?:\\\n.*)*", masked, re.M)
    if m:
        return text[m.start() : m.end()]
    return None


def _referenced_idents(body: str) -> set[str]:
    """Identifiers referenced in ``body`` (a definition), ignoring C keywords and
    tokens that appear only in comments/strings — those aren't real dependencies."""
    code = _mask_comments(body)
    return {w for w in re.findall(r"\b([A-Za-z_]\w*)\b", code) if w not in _KEYWORDS}


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
