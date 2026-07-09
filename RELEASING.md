# Releasing CGIR

CGIR ships to PyPI as **`codegraph-ir`**. The import package and the CLI
command are both `cgir` — only the distribution (PyPI project) name differs,
because `cgir` was already taken on PyPI. Nothing about the user-facing
`cgir` command changes.

## One-time setup

1. Create the PyPI project by uploading the first release (below); the name
   `codegraph-ir` is currently unclaimed.
2. Configure a PyPI API token (`~/.pypirc` or `UV_PUBLISH_TOKEN` /
   `TWINE_PASSWORD`). Prefer a **project-scoped** token after the first
   upload; a trusted-publisher GitHub Action is the eventual goal.

## Cut a release

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
