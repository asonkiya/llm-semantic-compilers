#!/bin/bash
set -euo pipefail
RUST="$1"; OUT="$2"; G="$(cd "$(dirname "$0")"&&pwd)"
VOL=cgir-kbuild; IMG=cgir-kernel-gate; GATE=crypto/cgir_gate
mkdir -p "$OUT"
docker run --rm -v "$VOL":/build "$IMG" bash -euc "cd /build/linux; mkdir -p $GATE; grep -q 'obj-y += cgir_gate/' crypto/Makefile || echo 'obj-y += cgir_gate/' >> crypto/Makefile"
run_leg(){ local leg="$1"
  docker run --rm -v "$VOL":/build "$IMG" bash -eo pipefail -uc "
    cd /build/linux; rm -f arch/arm64/boot/Image vmlinux
    make -s olddefconfig >/dev/null 2>&1||true; make -s -j\$(nproc) Image 2>&1|tail -3; test -f arch/arm64/boot/Image
    timeout 120 qemu-system-aarch64 -M virt -cpu max -m 1024 -nographic -net none -kernel arch/arm64/boot/Image -append 'console=ttyAMA0 panic=-1' -no-reboot 2>&1||true
  " > "$OUT/$leg-console.txt" 2>&1 || { echo "[$leg] BUILD FAILED"; tail -6 "$OUT/$leg-console.txt"; return 1; }
  grep CGIR_PROBE "$OUT/$leg-console.txt"|tail -1 > "$OUT/$leg-digest.txt"||true
  echo "[$leg] $(cat "$OUT/$leg-digest.txt" 2>/dev/null||echo '<none>')"; }
echo "=== STOCK (vli_cmp C) ==="
docker run --rm -v "$VOL":/build -v "$G":/g:ro "$IMG" bash -eo pipefail -uc "
  cd /build/linux/$GATE; cp /g/vli_probe.c cgir_fn_probe.c; cp /g/vli_cmp_stock.c cgir_fn_target.c
  rm -f cgir_fn_target_rust.o cgir_fn_target_rust.o_shipped
  printf 'obj-y := cgir_fn_probe.o cgir_fn_target.o\n' > Kbuild"
run_leg stock
echo "=== REWRITE (vli_cmp Rust: $(basename "$RUST")) ==="
docker run --rm -v "$VOL":/build -v "$G":/g:ro -v "$(cd "$(dirname "$RUST")"&&pwd)":/r:ro "$IMG" bash -eo pipefail -uc "
  cd /build/linux/$GATE; cp /g/vli_probe.c cgir_fn_probe.c
  rm -f cgir_fn_target.c cgir_fn_target.o cgir_fn_target_rust.o cgir_fn_target_rust.o_shipped
  rustc --target aarch64-unknown-none-softfloat --emit=obj -C panic=abort -C relocation-model=static -O /r/$(basename "$RUST") -o cgir_fn_target_rust.o_shipped
  test -s cgir_fn_target_rust.o_shipped
  printf 'obj-y := cgir_fn_probe.o cgir_fn_target_rust.o\n' > Kbuild" || { echo rustc-failed; exit 2; }
run_leg rewrite
S=$(sed -E 's/.*digest=//' "$OUT/stock-digest.txt" 2>/dev/null||true); R=$(sed -E 's/.*digest=//' "$OUT/rewrite-digest.txt" 2>/dev/null||true)
echo; echo "stock=$S rewrite=$R"
if [ -z "$S" ]||[ -z "$R" ]; then echo "GATE ERROR (vli_cmp)"; exit 2
elif [ "$S" = "$R" ]; then echo "GATE PASS (vli_cmp): Rust verified in-kernel"; exit 0
else echo "GATE REJECT (vli_cmp): diverged"; exit 1; fi
