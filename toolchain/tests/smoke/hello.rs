#![no_std]

unsafe extern "C" {
    fn puts(message: *const u8) -> i32;
}

#[unsafe(no_mangle)]
pub extern "C" fn rust_main() -> i32 {
    unsafe { puts(b"RUST SMOKE: 42\0".as_ptr()) };
    0
}
