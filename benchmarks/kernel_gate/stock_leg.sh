#!/bin/bash
# Rung-4 stock leg: build the real kernel (arm64 defconfig, built-in crypto
# self-tests) inside the gate container and boot it under QEMU, capturing the
# testmgr/boot output. That output — timestamp-normalized — is the golden
# stdout the Rust-linked build must reproduce byte-identically.
#
# The tree is COPIED into a docker volume first: kernel builds over a macOS
# bind mount are pathologically slow (gRPC-FUSE); the volume is container-
# native ext4 and survives across runs for incremental rebuilds.
#
# Usage: stock_leg.sh /path/to/linux-checkout /path/to/output-dir
set -euo pipefail
SRC="$1"
OUT="$2"
VOL=cgir-kbuild
IMG=cgir-kernel-gate
mkdir -p "$OUT"

docker volume create "$VOL" >/dev/null

# one-time copy of the tree into the volume (rsync-less: tar pipe, ~2 min)
if ! docker run --rm -v "$VOL":/build "$IMG" test -f /build/linux/Makefile; then
  echo "[stock] copying kernel tree into volume..."
  docker run --rm -v "$SRC":/src:ro -v "$VOL":/build "$IMG" \
    bash -c "mkdir -p /build/linux && tar -C /src --exclude=.git -cf - . | tar -C /build/linux -xf -"
fi

echo "[stock] configuring (defconfig + built-in crypto self-tests on boot)..."
docker run --rm -v "$VOL":/build "$IMG" bash -c "
  cd /build/linux &&
  make -s defconfig &&
  ./scripts/config -e CRYPTO_MANAGER -d CRYPTO_MANAGER_DISABLE_TESTS \
    -e CRYPTO_SELFTESTS -e CRYPTO_NULL -e CRYPTO_CBC -e CRYPTO_ECB \
    -e CRYPTO_SHA256 -e CRYPTO_SHA512 -e CRYPTO_AES -e IKCONFIG &&
  make -s olddefconfig
"

echo "[stock] building Image (-j\$(nproc))..."
docker run --rm -v "$VOL":/build "$IMG" bash -c "
  cd /build/linux && make -s -j\$(nproc) Image 2>&1 | tail -5
"

echo "[stock] booting under QEMU, capturing console (120s cap)..."
docker run --rm -v "$VOL":/build "$IMG" bash -c "
  timeout 120 qemu-system-aarch64 -M virt -cpu max -m 1024 -nographic \
    -kernel /build/linux/arch/arm64/boot/Image \
    -append 'console=ttyAMA0 panic=-1' -no-reboot 2>&1 || true
" > "$OUT/stock-console.txt"

# normalized golden: strip timestamps + addresses; keep the semantic lines
sed -E 's/^\[[0-9. ]+\] //; s/0x[0-9a-f]+/0xADDR/g' "$OUT/stock-console.txt" \
  > "$OUT/stock-console.normalized.txt"
grep -cE "alg: .*(passed|self-tests)|Freeing unused kernel" "$OUT/stock-console.txt" \
  > "$OUT/stock-markers.txt" || true
echo "[stock] done: $(wc -l < "$OUT/stock-console.txt") console lines, markers: $(cat "$OUT/stock-markers.txt")"
