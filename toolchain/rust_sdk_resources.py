"""Generate portable packaged SDK definitions from pinned Rust-APE templates.

The pinned Cargo resolves these paths from the config's grandparent. Verify this
with check_cargo_config_paths.py when upgrading Cargo; no host paths are serialized.
"""

import json
from pathlib import Path


def portable_rust_resources(sdk: Path) -> dict[str, tuple[bytes, int]]:
    if not (sdk / "scripts/gcc-linker-wrapper.bash").is_file():
        raise ValueError("Rust-APE linker source is missing")
    templates = sorted((sdk / "targets").glob("*.json"))
    if not templates:
        raise ValueError("Rust-APE target templates are missing")
    resources = {}
    # Cargo's private std-source override must be set in the invoking process;
    # [env] entries only configure the subprocesses that Cargo launches.
    config = ["[env]"]
    targets = []
    script_header = ('#!/bin/sh\nset -eu\n'
                     'sdk_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n')
    flags = ['--cfg', 'rustix_use_libc', '--cfg', 'polling_test_poll_backend',
             '--cfg', 'mio_unsupported_force_waker_pipe', '--cfg', 'rust_ape_shim']
    for template in templates:
        target = json.loads(template.read_text())
        arch = target["arch"]
        triple = template.stem
        if arch not in ("x86_64", "aarch64") or triple != f"{arch}-unknown-linux-musl":
            raise ValueError(f"Unsupported Rust-APE target: {triple}")
        if target.get("linker") != "@APE_LINKER@":
            raise ValueError(f"Expected linker placeholder in {template.name}")
        target["linker"] = f"linker-{arch}.bash"
        resources[f"generated/{triple}.json"] = ((json.dumps(target, indent=2) + "\n").encode(), 0o644)
        linker = script_header + f'export ARCH={arch}\nexec "$sdk_root/scripts/gcc-linker-wrapper.bash" "$@"\n'
        resources[f"generated/linker-{arch}.bash"] = (linker.encode(), 0o755)
        ar = script_header + f'exec sh "$sdk_root/vendor/cosmocc/bin/{arch}-unknown-cosmo-ar" "$@"\n'
        resources[f"generated/ar-{arch}.bash"] = (ar.encode(), 0o755)
        for name, executable in (("cc", "cc"), ("cxx", "c++")):
            command = (f'exec "$sdk_root/vendor/cosmocc/bin/{arch}-unknown-cosmo-{executable}" '
                       '-fno-stack-protector -mstack-protector-guard=global -D_GNU_SOURCE '
                       '-DSOCK_CLOEXEC=SOCK_CLOEXEC -DMADV_FREE=MADV_FREE -DHAVE_ENDIAN_H '
                       '-include "$sdk_root/scripts/cdeps-predef.h" "$@"\n')
            resources[f"generated/{name}-{arch}.bash"] = ((script_header + command).encode(), 0o755)
        key = triple.replace("-", "_")
        for variable, script in (("CC", "cc"), ("CXX", "cxx"), ("AR", "ar")):
            config.append(f'{variable}_{key} = {{ value = "generated/{script}-{arch}.bash", relative = true, force = true }}')
        targets += [f'\n[target.{triple}]', f'linker = "generated/linker-{arch}.bash"',
                    f'rustflags = {json.dumps(flags)}']
    config += targets
    config += ['\n[patch.crates-io]',
               'libc = { path = "vendor/patches/libc" }',
               'errno = { path = "vendor/patches/errno" }']
    resources["generated/cargo.cosmo.toml"] = (("\n".join(config) + "\n").encode(), 0o644)
    return resources
