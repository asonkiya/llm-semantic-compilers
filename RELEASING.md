# Releasing CGIR

CGIR ships to PyPI as **`codegraph-ir`**. The import package and the CLI
command are both `cgir` — only the distribution (PyPI project) name differs,
because `cgir` was already taken on PyPI. Nothing about the user-facing
`cgir` command changes.

## Preferred: tag-triggered release (no token handling)

`.github/workflows/release.yml` publishes on any `v*` tag via **PyPI
trusted publishing** (OIDC — no secret stored anywhere). It gates on
tests/lint/types, refuses a tag that doesn't match `cgir.__version__`,
smoke-tests the wheel in a clean env, then uploads.

One-time setup on PyPI (works for the *first* release too, via a
"pending publisher"):

1. PyPI → your account → Publishing → **Add a pending publisher**:
   project `codegraph-ir`, owner `asonkiya`, repo `llm-semantic-compilers`,
   workflow `release.yml`, environment `pypi`.
2. GitHub repo → Settings → Environments → create `pypi` (optionally with
   required reviewers, making every release a manual approval).

Then a release is:

```bash
# bump version in pyproject.toml AND src/cgir/__init__.py, commit, then:
git tag -a v0.1.0 -m "release" && git push origin v0.1.0
```

## Fallback: manual release

```bash
# 1. bump the version in BOTH places (they must match):
#    - pyproject.toml            [project] version
#    - src/cgir/__init__.py      __version__
# 2. green gate:
pytest -q && ruff check . && ruff format --check . && mypy src

# 3. build sdist + wheel:
rm -rf dist && uv build

# 4. verify the artifact installs and the CLI runs from a clean env:
uv venv /tmp/cgir-rel && \
  uv pip install --python /tmp/cgir-rel/bin/python dist/codegraph_ir-*.whl && \
  /tmp/cgir-rel/bin/cgir --version

# 5. publish:
uv publish            # or: twine upload dist/*

# 6. tag:
git tag -a "v$(python -c 'import cgir; print(cgir.__version__)')" -m "release" && git push --tags
```

## What the wheel contains (verified)

- console script `cgir = cgir.cli:app`
- `cgir/py.typed` (PEP 561 — the package ships type information)
- runtime deps only (typer, pydantic, networkx, tree-sitter + Python/TS
  grammars, jsonschema); `dev`/`api`/`llm`/`mcp` are opt-in extras

## GitHub Action

`action.yml` installs `codegraph-ir` by default (the `cgir-version` input).
To let people `uses: asonkiya/llm-semantic-compilers@v0`, push a `v0` tag
(and keep it moving) or publish to the GitHub Marketplace.

## Version policy

`0.y.z` while the contract vocabulary (`NodeKind`/`EdgeKind`/`ComponentSpec`)
may still shift. The index also carries its own schema version in
`manifest.json` (`cgir --version` and `compatibility_warning`), independent
of the package version.
