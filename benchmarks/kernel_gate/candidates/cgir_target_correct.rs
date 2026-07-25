// Correct Rust rewrite of cgir_target — the FNV-1a mix, byte-for-byte the C
// semantics. Pure, #![no_std], no kernel API: compiles to a freestanding
// aarch64 object that links into vmlinux with no CONFIG_RUST machinery.
#![no_std]
#![no_main]

#[panic_handler]
fn ph(_: &core::panic::PanicInfo) -> ! {
    loop {}
}

#[no_mangle]
pub extern "C" fn cgir_target(x: u64) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    let mut v = x;
    let mut i = 0;
    while i < 8 {
        h ^= v & 0xff;
        h = h.wrapping_mul(0x100000001b3);
        v >>= 8;
        i += 1;
    }
    h
}
