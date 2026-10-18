"""Small native-format fixtures; none of these files are runnable compilers."""

from bootstrap import ELF_TARGETS, RUST_NOTICES


def binary_fixture(host):
    header = bytearray(160)
    if "windows" in host:
        header[:2] = b"MZ"
        header[0x3c] = 128
        header[128:134] = b"PE\0\0\x64\x86"
    elif "apple" in host:
        header[:4] = b"\xcf\xfa\xed\xfe"
        header[4:8] = (0x0100000c if host.startswith("aarch64") else 0x01000007).to_bytes(4, "little")
    else:
        header[:6] = b"\x7fELF\x02\x01"
        header[18] = 183 if host.startswith("aarch64") else 62
    return bytes(header)


def make_sysroot(root, host):
    source = root / ("rust-source-" + host)
    (source / "bin").mkdir(parents=True)
    (source / "lib/rustlib").mkdir(parents=True)
    for name in ("rustc", "cargo"):
        file = source / "bin" / (name + (".exe" if "windows" in host else ""))
        file.write_bytes(binary_fixture(host))
        file.chmod(0o755)
    if "windows" in host:
        (source / "bin/rustc_driver-fixture.dll").write_bytes(b"native DLL")
    else:
        (source / "lib/librustc_driver.so").write_text("compiler library")
    for target in set((*ELF_TARGETS, host)):
        lib = source / "lib/rustlib" / target / "lib"
        lib.mkdir(parents=True)
        (lib / "libcore.rlib").write_text("shared target " + target)
    tools = source / "lib/rustlib" / host / "bin"
    tools.mkdir()
    (tools / "rust-lld").write_text("native linker " + host)
    (source / "lib/rustlib/multirust-channel-manifest.toml").write_text(
        'date = "2026-07-28"\n[pkg.rustc]\nversion = "pinned rustc"\n[pkg.cargo]\nversion = "pinned cargo"\n')
    for name in RUST_NOTICES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("notice " + name)
    return source
