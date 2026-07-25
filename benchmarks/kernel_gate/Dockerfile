# Kernel whole-program-gate environment (rung 4): build the REAL kernel with a
# rewritten function linked in, boot it under QEMU, and byte-compare the
# kernel's own crypto self-test output (testmgr) against stock. macOS can't
# run kbuild natively; this arm64 container can, and Apple-silicon Docker runs
# it at native speed. QEMU boots with TCG (no nested virt) — fine for a
# boot-to-testmgr run.
FROM debian:bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential flex bison bc libssl-dev libelf-dev \
    python3 curl ca-certificates git kmod cpio \
    qemu-system-arm \
    && rm -rf /var/lib/apt/lists/*

# Rust for the no_std staticlib leg (the rewritten fn compiled --emit=obj and
# linked into vmlinux via kbuild's obj-y).
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain stable --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /kernel
