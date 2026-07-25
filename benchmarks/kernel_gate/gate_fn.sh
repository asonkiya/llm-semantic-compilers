#!/bin/bash
# Real-function in-kernel gate. Verifies a model-generated Rust rewrite of a real
# kernel u32->u32 function against its C original, by boot digest, inside vmlinux.
# Usage: gate_fn.sh <fn> <stock_c_file> <rust_file> <out_dir>
set -euo pipefail
FN="$1"; STOCK_C="$2"; RUST="$3"; OUT="$4"
G="$(cd "$(dirname "$0")" && pwd)"
VOL=cgir-kbuild; IMG=cgir-kernel-gate; GATE=crypto/cgir_gate
mkdir -p "$OUT"
sed "s/@FN@/$FN/g" "$G/fn_probe.c.tmpl" > "$OUT/cgir_fn_probe.c"

# wire probe subdir into crypto/Makefile once
docker run --rm -v "$VOL":/build "$IMG" bash -euc "cd /build/linux; mkdir -p $GATE; grep -q 'obj-y += cgir_gate/' crypto/Makefile || echo 'obj-y += cgir_gate/' >> crypto/Makefile"

run_leg() {
  local leg="$1"
  docker run --rm -v "$VOL":/build "$IMG" bash -eo pipefail -uc "
    cd /build/linux
    rm -f arch/arm64/boot/Image vmlinux
    make -s olddefconfig >/dev/null 2>&1 || true
    make -s -j\$(nproc) Image 2>&1 | tail -4
    test -f arch/arm64/boot/Image
    timeout 120 qemu-system-aarch64 -M virt -cpu max -m 1024 -nographic -net none \
      -kernel arch/arm64/boot/Image -append 'console=ttyAMA0 panic=-1' -no-reboot 2>&1 || true
  " > "$OUT/$leg-console.txt" 2>&1 || { echo "[$leg] BUILD FAILED"; tail -6 "$OUT/$leg-console.txt"; return 1; }
  grep -E 'CGIR_PROBE' "$OUT/$leg-console.txt" | tail -1 > "$OUT/$leg-digest.txt" || true
  echo "[$leg] $(cat "$OUT/$leg-digest.txt" 2>/dev/null || echo '<no digest>')"
}

echo "=== STOCK ($FN in C) ==="
docker run --rm -v "$VOL":/build -v "$OUT":/o:ro -v "$(cd "$(dirname "$STOCK_C")"&&pwd)":/s:ro "$IMG" bash -eo pipefail -uc "
  cd /build/linux/$GATE
  cp /o/cgir_fn_probe.c cgir_fn_probe.c
  # de-static the target so the probe (separate TU) links to it
  sed 's/^static \(u32 $FN\)/\1/' /s/$(basename "$STOCK_C") > cgir_fn_target.c
  rm -f cgir_fn_target_rust.o cgir_fn_target_rust.o_shipped
  printf 'obj-y := cgir_fn_probe.o cgir_fn_target.o\n' > Kbuild
"
run_leg stock

echo "=== REWRITE ($FN in Rust: $(basename "$RUST")) ==="
docker run --rm -v "$VOL":/build -v "$OUT":/o:ro -v "$(cd "$(dirname "$RUST")"&&pwd)":/r:ro "$IMG" bash -eo pipefail -uc "
  cd /build/linux/$GATE
  cp /o/cgir_fn_probe.c cgir_fn_probe.c
  rm -f cgir_fn_target.c cgir_fn_target.o cgir_fn_target_rust.o cgir_fn_target_rust.o_shipped
  rustc --target aarch64-unknown-none-softfloat --emit=obj -C panic=abort -C relocation-model=static -O /r/$(basename "$RUST") -o cgir_fn_target_rust.o_shipped
  test -s cgir_fn_target_rust.o_shipped
  printf 'obj-y := cgir_fn_probe.o cgir_fn_target_rust.o\n' > Kbuild
" || { echo 'rustc failed'; exit 2; }
run_leg rewrite

S=$(sed -E 's/.*digest=//' "$OUT/stock-digest.txt" 2>/dev/null||true)
R=$(sed -E 's/.*digest=//' "$OUT/rewrite-digest.txt" 2>/dev/null||true)
echo; echo "stock=$S rewrite=$R"
if [ -z "$S" ]||[ -z "$R" ]; then echo "GATE ERROR ($FN): missing digest"; exit 2
elif [ "$S" = "$R" ]; then echo "GATE PASS ($FN): Rust verified in-kernel"; exit 0
else echo "GATE REJECT ($FN): diverged"; exit 1; fi
