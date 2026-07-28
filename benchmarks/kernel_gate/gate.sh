#!/bin/bash
# CGIR rung-4 whole-kernel gate. Builds+boots Linux twice — stock (C target)
# and rewrite (Rust target linked in place of the C definition) — and compares
# the probe's deterministic boot digest. A rewrite that changes behavior
# changes the digest and is REJECTED, exactly like the SQLite whole-program
# gate, with the Linux kernel as the program.
#
# Usage: gate.sh <candidate.rs> [out-dir]
#   verifies <candidate.rs> against the stock C cgir_target.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CAND="${1:?usage: gate.sh <candidate.rs> [out-dir]}"
OUT="${2:-./kernel-gate-out}"
VOL=cgir-kbuild
IMG=cgir-kernel-gate
mkdir -p "$OUT"
GATE=crypto/cgir_gate  # a self-contained kbuild subdir inside the kernel tree

# --- one-time: install the probe subdir + wire it into crypto/Makefile -------
docker run --rm -v "$VOL":/build -v "$HERE":/gate:ro "$IMG" bash -euc "
  cd /build/linux
  mkdir -p $GATE
  cp /gate/probe/cgir_probe.c $GATE/cgir_probe.c
  grep -q 'obj-y += cgir_gate/' crypto/Makefile || echo 'obj-y += cgir_gate/' >> crypto/Makefile
"

# --- helper: build one leg (Kbuild body decides target obj) and boot ---------
run_leg() {  # $1 = leg name ; expects $GATE prepared
  local leg="$1"
  # Delete the prior Image so a FAILED rebuild can't silently boot a stale
  # kernel and fake a match (the bug the negative control caught). The build
  # must regenerate Image or the leg errors out.
  docker run --rm -v "$VOL":/build "$IMG" bash -eo pipefail -uc "
    cd /build/linux
    rm -f arch/arm64/boot/Image vmlinux
    make -s olddefconfig >/dev/null 2>&1 || true
    make -s -j\$(nproc) Image 2>&1 | tail -4
    test -f arch/arm64/boot/Image  # hard-fail the leg if the build produced no kernel
    timeout 120 qemu-system-aarch64 -M virt -cpu max -m 1024 -nographic -net none \
      -kernel arch/arm64/boot/Image \
      -append 'console=ttyAMA0 panic=-1' -no-reboot 2>&1 || true
  " > "$OUT/$leg-console.txt" 2>&1 || { echo "[$leg] BUILD FAILED"; tail -6 "$OUT/$leg-console.txt"; return 1; }
  grep -E 'CGIR_PROBE' "$OUT/$leg-console.txt" | tail -1 > "$OUT/$leg-digest.txt" || true
  echo "[$leg] $(cat "$OUT/$leg-digest.txt" 2>/dev/null || echo '<no digest — boot failed>')"
}

# --- stock leg: C target ------------------------------------------------------
echo "=== STOCK leg (C cgir_target) ==="
docker run --rm -v "$VOL":/build -v "$HERE":/gate:ro "$IMG" bash -euc "
  cd /build/linux/$GATE
  cp /gate/probe/cgir_target.c cgir_target.c
  rm -f cgir_target_rust.o cgir_target_rust.o_shipped
  printf 'obj-y := cgir_probe.o cgir_target.o\n' > Kbuild
"
run_leg stock

# --- rewrite leg: Rust target (C definition dropped) -------------------------
echo "=== REWRITE leg (Rust cgir_target: $(basename "$CAND")) ==="
docker run --rm -v "$VOL":/build -v "$HERE":/gate:ro -v "$(cd "$(dirname "$CAND")" && pwd)":/cand:ro "$IMG" bash -eo pipefail -uc "
  cd /build/linux/$GATE
  rm -f cgir_target.c cgir_target.o cgir_target_rust.o cgir_target_rust.o_shipped
  # rustc failure must abort the leg (no pipe to swallow the exit code) — the
  # gate is meaningless if a candidate that won't compile still 'passes'.
  rustc --target aarch64-unknown-none-softfloat --emit=obj -C panic=abort \
    -C relocation-model=static -O /cand/$(basename "$CAND") -o cgir_target_rust.o_shipped
  test -s cgir_target_rust.o_shipped
  printf 'obj-y := cgir_probe.o cgir_target_rust.o\n' > Kbuild
" || { echo "REWRITE leg: rustc failed to build the candidate"; exit 2; }
run_leg rewrite

# --- verdict -----------------------------------------------------------------
S=$(sed -E 's/.*digest=//' "$OUT/stock-digest.txt" 2>/dev/null || true)
R=$(sed -E 's/.*digest=//' "$OUT/rewrite-digest.txt" 2>/dev/null || true)
echo
echo "stock  digest: ${S:-<none>}"
echo "rewrite digest: ${R:-<none>}"
if [ -z "$S" ] || [ -z "$R" ]; then
  echo "GATE ERROR: a leg produced no digest (boot failed)"; exit 2
elif [ "$S" = "$R" ]; then
  echo "GATE PASS: byte-identical boot digest — Rust cgir_target verified in-kernel"; exit 0
else
  echo "GATE REJECT: digest diverged — rewrite changed behavior"; exit 1
fi
