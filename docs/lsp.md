# LSP diagnostics — contract drift in your editor

`cgir lsp` is a diagnostics-only language server: on every save it rescans
the repo and publishes

- **errors** for pin violations (`# cgir: pure` that isn't) — visible the
  moment you open the workspace;
- **warnings** for contract drift vs your previous save (the default
  low-noise gate rules: effect gain/loss on net/fs/db, change pins).

Side effect worth having: every refresh rewrites `.cgir`, so MCP, `pack`,
and `impact` stay fresh while you edit.

```bash
uv tool install "codegraph-ir[lsp]"    # or add the lsp extra to your env
```

## Neovim

```lua
vim.api.nvim_create_autocmd("FileType", {
  pattern = { "python", "typescript", "go" },
  callback = function()
    vim.lsp.start({ name = "cgir", cmd = { "cgir", "lsp" },
                    root_dir = vim.fs.root(0, ".cgir") or vim.fn.getcwd() })
  end,
})
```

## VS Code

Use any generic LSP client extension (e.g. "LSP Proxy"/"Generic LSP
Client") with command `cgir lsp`, or wire it into an existing extension's
`serverCommand` setting. A dedicated extension is on the roadmap.

## Latency honesty

Each save triggers a full rescan (~0.5s small repos, ~3–5s large ones).
Diagnostics update when the scan lands; per-component incremental analysis
(docs/plan-0.3.md) is the planned fix.
