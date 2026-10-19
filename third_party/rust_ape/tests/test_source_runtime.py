"""Source contracts complement the driver's compile and cross-host checks."""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
SHIM = ROOT / "third_party/rust_ape/upstream/shim"


class SourceRuntimeTests(unittest.TestCase):
    def test_sid_authority_has_one_definition(self):
        header = (ROOT / "libc/nt/struct/sid.h").read_text()
        self.assertEqual(header.count("struct NtSidIdentifierAuthority {"), 1)

    def test_no_shim_uses_the_retired_global_descriptor_table(self):
        for path in SHIM.rglob("*.c"):
            with self.subTest(path=path.name):
                self.assertNotIn("g_fds.", path.read_text())

    def test_superseded_overrides_are_removed_not_disabled(self):
        for name in ("createfileflags.c", "dlmalloc.c", "open-nt.c", "close-nt.c", "describefds.c"):
            self.assertFalse((SHIM / name).exists())
        self.assertIn("__isthreaded >= 2", (ROOT / "third_party/dlmalloc/locks.inc").read_text())
        self.assertIn("kNtFileFlagWriteThrough", (ROOT / "libc/calls/createfileflags.c").read_text())

    def test_fork_retains_new_process_state_and_reservation_copy(self):
        source = (SHIM / "fork-nt.c").read_text()
        for contract in ("sys_fork_nt(void)", "__get_pib()->pid = child_pid", "proc->hStopEvent", "CopyReservation("):
            self.assertIn(contract, source)
        for retired in ("proc->wasforked", "proc->handle", "__morph_tls()", "MAPS_SIZE"):
            self.assertNotIn(retired, source)

    def test_close_keeps_atomic_ownership_and_procfs_cleanup(self):
        source = (SHIM / "close.c").read_text()
        self.assertIn("atomic_exchange(&f->kind, kFdReserved)", source)
        self.assertIn("__ape_shim_procfs_memfd_close(fd)", source)
        self.assertIn("__ape_shim_nt_sockopt_forget(fd)", source)
        self.assertIn("f->was_created_during_vfork", source)

    def test_native_child_status_adaptation_preserves_vfork(self):
        source = (SHIM / "proc.c").read_text()
        self.assertIn("pr->hProcess2", source)
        self.assertIn("!pr->isvfork && __proc_is_native_child(pr->hProcess)", source)
        self.assertIn("__proc_native_wstatus(dwExitCode)", source)

    def test_fchdir_uses_the_source_runtime_chdir_entrypoint(self):
        source = (SHIM / "fchdir-nt.c").read_text()
        self.assertNotIn("sys_chdir_nt_impl", source)
        self.assertIn("return sys_chdir_nt16(dir, len)", source)
        self.assertIn("int sys_chdir_nt16(", (ROOT / "libc/calls/chdir-nt.c").read_text())

    def test_dladdr_is_independent_of_other_loader_stubs(self):
        self.assertNotIn("int dladdr(", (ROOT / "libc/dlopen/stubs.c").read_text())
        self.assertIn("int dladdr(", (ROOT / "libc/dlopen/dladdr.c").read_text())
        self.assertIn("void *dlopen(", (ROOT / "libc/dlopen/stubs.c").read_text())

    def test_descriptor_handoff_uses_the_runtime_extra_handles_contract(self):
        self.assertFalse((SHIM / "describefds.c").exists())
        source = (ROOT / "libc/proc/describefds.c").read_text()
        self.assertIn("long *opt_inout_lpExtraHandles", source)
        self.assertIn("__undescribe_fds(envstr.p, hCreatorProcess, handles.p, handles.n)", source)


if __name__ == "__main__":
    unittest.main()
