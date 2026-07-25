// Negative control — a plausible-but-wrong rewrite: the FNV prime is off by
// one bit (0x...1b3 -> 0x...1b2). Compiles and links fine; must produce a
// different boot digest and be REJECTED by the gate. Proves the pass is not
// vacuous (the probe actually exercises the swapped code).
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
        h = h.wrapping_mul(0x100000001b2); // wrong prime
        v >>= 8;
        i += 1;
    }
    h
}
