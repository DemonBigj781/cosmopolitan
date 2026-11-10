"""Host-only fixtures for source foundation export.

No compiler, runtime probe, application, inference or GPU is launched. Set
EASY_DIFFUSION_CONSUMER_DIR to opt into the separate consumer contract checks.
"""
import copy
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "toolchain"))
import export_foundation as exporter
from runtime_identity import inventory


def archive_bytes():
    payload = b"fixture object\n"
    return (b"!<arch>\n" + b"fixture.o/".ljust(16) + b"0".ljust(12)
            + b"0".ljust(6) + b"0".ljust(6) + b"100644".ljust(8)
            + str(len(payload)).encode().ljust(10) + b"`\n" + payload
            + (b"\n" if len(payload) % 2 else b""))


class FoundationExportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write(self, name, data=b"fixture", mode=0o644):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def test_materializes_directory_and_file_aliases_preserving_target_modes(self):
        source = self.root / "source"
        self.write("source/real/compiler", b"compiler", 0o755)
        (source / "bin").symlink_to("real", target_is_directory=True)
        (source / "cc").symlink_to("bin/compiler")
        record = {"files": inventory(source)}
        raw = json.dumps(record, indent=3).encode() + b"\n"
        self.write("source/SOURCE_RUNTIME.json", raw)
        output = self.root / "sdk"
        exporter.materialize_sdk(source, output, record)
        self.assertEqual((output / "SOURCE_RUNTIME.json").read_bytes(), raw)
        self.assertEqual((output / "cc").read_bytes(), b"compiler")
        self.assertEqual((output / "cc").stat().st_mode & 0o777, 0o755)
        self.assertFalse(any(path.is_symlink() for path in output.rglob("*")))
        self.assertEqual(set(exporter.regular_files(output)),
                         {"real/compiler", "bin/compiler", "cc", "SOURCE_RUNTIME.json"})

    def test_alias_resolution_expands_links_before_parent_components(self):
        self.write("sdk/real/child/entry", b"child")
        self.write("sdk/real/entry", b"correct")
        self.write("sdk/entry", b"incorrect")
        (self.root / "sdk/jump").symlink_to("real/child", target_is_directory=True)
        (self.root / "sdk/alias").symlink_to("jump/../entry")
        result = exporter.materialized_sdk_files(inventory(self.root / "sdk"))
        self.assertEqual(result["alias"][0], "real/entry")

    def test_rejects_file_and_directory_cycles_escapes_and_dangling_aliases(self):
        def alias(target):
            return {"type": "symlink", "mode": 0o777, "target": target,
                    "sha256": hashlib.sha256(target.encode()).hexdigest()}
        cases = ({"a": alias("a")}, {"a": alias("b"), "b": alias("a")},
                 {"a": alias("../outside")}, {"a": alias("missing")},
                 {"directory": {"type": "directory", "mode": 0o755},
                  "directory/root": alias("..")})
        for records in cases:
            with self.subTest(records=records), self.assertRaises(ValueError):
                exporter.materialized_sdk_files(records)

    def test_materialized_sdk_rejects_missing_extra_changed_files_and_modes(self):
        source = self.root / "source"
        self.write("source/file", b"content", 0o755)
        record = {"files": inventory(source)}
        self.write("source/SOURCE_RUNTIME.json", json.dumps(record).encode())
        output = self.root / "sdk"
        exporter.materialize_sdk(source, output, record)
        (output / "file").chmod(0o644)
        with self.assertRaises(ValueError):
            exporter.verify_materialized_sdk(output, record)
        (output / "file").chmod(0o755)
        (output / "extra").write_bytes(b"extra")
        with self.assertRaises(ValueError):
            exporter.verify_materialized_sdk(output, record)
        (output / "extra").unlink()
        (output / "file").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            exporter.verify_materialized_sdk(output, record)
        (output / "file").unlink()
        with self.assertRaises(ValueError):
            exporter.verify_materialized_sdk(output, record)

    def test_empty_sdk_stubs_are_data_but_runtime_archives_must_be_nonempty(self):
        empty = self.write("empty.a", b"!<arch>\n")
        self.assertEqual(exporter.artifact_kind(empty, sdk=True), "data")
        with self.assertRaises(ValueError):
            exporter.artifact_kind(empty)
        archive = self.write("full.a", archive_bytes())
        self.assertEqual(exporter.artifact_kind(archive, sdk=True), "archive")
        thin = self.write("thin.a", b"!<thin>\n")
        with self.assertRaises(ValueError):
            exporter.artifact_kind(thin, sdk=True)

    def test_rejects_invalid_gnu_archive_name_table_references(self):
        invalid = archive_bytes().replace(b"fixture.o/".ljust(16), b"/0".ljust(16), 1)
        path = self.write("missing-names.a", invalid)
        with self.assertRaisesRegex(ValueError, "name-table"):
            exporter.artifact_kind(path)

    def test_rejects_mislabeled_rust_target_and_changed_spec(self):
        sdk = self.root / "rust-sdk"
        target = self.write("rust-sdk/generated/x86_64-unknown-linux-musl.json", b"actual target spec")
        alternate = self.write("rust-sdk/generated/x86_64-alternate.json", b"alternate target spec")
        checksum = exporter.digest_file(target)
        build = {"rust_target": target.stem, "rust_target_spec_sha256": checksum}
        invocation = {"environment": {"CARGO_BUILD_TARGET": str(target)}, "target_sha256": checksum}
        self.assertEqual(exporter.verified_rust_target(build, invocation, sdk), (target.stem, target))
        bad_builds = ({**build, "rust_target": alternate.stem},
                      {**build, "rust_target_spec_sha256": exporter.digest_file(alternate)})
        for bad in bad_builds:
            with self.subTest(build=bad), self.assertRaises(ValueError):
                exporter.verified_rust_target(bad, invocation, sdk)
        bad_invocations = ({**invocation, "environment": {"CARGO_BUILD_TARGET": str(alternate)}},
                           {**invocation, "target_sha256": exporter.digest_file(alternate)})
        for bad in bad_invocations:
            with self.subTest(invocation=bad), self.assertRaises(ValueError):
                exporter.verified_rust_target(build, bad, sdk)
        target.write_bytes(b"changed actual target spec")
        with self.assertRaises(ValueError):
            exporter.verified_rust_target(build, invocation, sdk)

    def test_rejects_mislabeled_llvm_target(self):
        pins = {"target_triple": "x86_64-unknown-linux-musl"}
        self.assertEqual(exporter.verified_c_target(dict(pins), pins), pins["target_triple"])
        with self.assertRaises(ValueError):
            exporter.verified_c_target({"target_triple": "x86_64-alternate"}, pins)

    def test_export_does_not_replace_existing_output(self):
        for name in ("repository", "sdk", "rust", "output"):
            (self.root / name).mkdir()
        self.write("output/sentinel", b"untouched")
        with patch.object(exporter, "verify_inputs") as verify:
            with self.assertRaises(ValueError):
                exporter.export_foundation(self.root / "repository", self.root / "sdk",
                                           self.root / "rust", self.root / "output")
            verify.assert_not_called()
        self.assertEqual((self.root / "output/sentinel").read_bytes(), b"untouched")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux no-replacement publication")
    def test_atomic_publication_refuses_even_empty_existing_directory(self):
        source, destination = self.root / "source", self.root / "destination"
        source.mkdir()
        destination.mkdir()
        self.write("source/sentinel", b"new")
        with self.assertRaises(OSError):
            exporter.publish_new_directory(source, destination)
        self.assertTrue((source / "sentinel").is_file())
        self.assertEqual(list(destination.iterdir()), [])
        destination.rmdir()
        exporter.publish_new_directory(source, destination)
        self.assertEqual((destination / "sentinel").read_bytes(), b"new")

    def test_staging_failure_cleans_temporary_output(self):
        for name in ("repository", "sdk", "rust"):
            (self.root / name).mkdir()
        with patch.object(exporter, "verify_inputs", return_value={"record": {}}), \
                patch.object(exporter, "candidate_pin", return_value={}), \
                patch.object(exporter, "materialize_sdk"), \
                patch.object(exporter, "stage_webgpu_sdk", side_effect=ValueError("incomplete")):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                exporter.export_foundation(self.root / "repository", self.root / "sdk",
                                           self.root / "rust", self.root / "output")
        self.assertFalse((self.root / "output").exists())
        self.assertEqual(list(self.root.glob(".foundation-export-*")), [])

    def assembly_fixture(self):
        stage = self.root / "export"
        sdk = stage / "sdk"
        for filename in exporter.TOOLS.values():
            self.write("export/sdk/bin/" + filename, b"#!/bin/sh\nexit 1\n", 0o755)
        self.write("export/sdk/include/stdio.h", b"/* fixture */")
        self.write("export/sdk/x86_64-linux-cosmo/lib/crt.o", b"object")
        for filename in ("libcosmo.a", "libcxx.a"):
            self.write("export/sdk/x86_64-linux-cosmo/lib/" + filename, archive_bytes())
        self.write("export/sdk/x86_64-linux-cosmo/lib/libm.a", b"!<arch>\n")
        source = {"repository": "https://github.com/example/foundation.git",
                  "revision": "0123456789abcdef0123456789abcdef01234567",
                  "tree": hashlib.sha1(b"synthetic tree").hexdigest(), "clean": True}
        abi_paths = ("libc/nt/struct/sid.h", "libc/dlopen/dladdr.c", "libc/intrin/fds.h", "libc/proc/posix_spawn.c")
        source_files = {}
        for name in (*abi_paths, "tool/cosmocc/package.sh"):
            content = ("synthetic source: " + name).encode()
            source_files[name] = {"mode": "100644", "type": "file", "size": len(content),
                                  "sha256": hashlib.sha256(content).hexdigest()}
        source_inventory = {"schema_version": 1, "kind": "cosmopolitan-tracked-source-inputs",
                            "source": source, "files": source_files}
        inventory_file = self.write("export/sdk/SOURCE_INPUTS.json", json.dumps(source_inventory).encode())
        record = {"schema_version": 1, "architectures": ["x86_64", "aarch64"],
                  "source": source, "source_patches": [],
                  "source_tree_sha256": exporter.canonical_hash(source_files),
                  "source_inventory_record": {"path": "SOURCE_INPUTS.json", "sha256": exporter.digest_file(inventory_file)},
                  "source_abi_inputs": {name: source_files[name]["sha256"] for name in abi_paths},
                  "bootstrap": {"role": "stock-compiler-input-only", "tree_sha256": exporter.canonical_hash("bootstrap tree"),
                                "archive_sha256": exporter.canonical_hash("bootstrap archive")},
                  "producer": {"entrypoint": "tool/cosmocc/package.sh", "fresh_outputs": True,
                               "sha256": source_files["tool/cosmocc/package.sh"]["sha256"]},
                  "kind": "source-built-cosmopolitan-runtime", "runtime_abi": "cosmopolitan-source-runtime-v1",
                  "files": inventory(sdk)}
        record["runtime_identity"] = "sha256:" + exporter.canonical_hash(record)
        self.write("export/sdk/SOURCE_RUNTIME.json", json.dumps(record).encode())
        for name in ("wgpu/libwgpu_native.a", "mesa/libregistration.a", "mesa/libmesa.a", "llvm/libLLVMCore.a"):
            self.write("export/dependencies/lib/" + name, archive_bytes())
        self.write("export/dependencies/lib/rust-ape/shim-test-x86_64.o", b"shim")
        for name in ("vulkan_loader.c", "win64_bridge.c", "vulkan_loader.h", "win64_bridge.h", "vulkan_signatures.inc"):
            self.write("export/dependencies/runtime/" + name)
        self.write("export/dependencies/include/webgpu.h")
        self.write("export/dependencies/licenses/LICENSE")
        runtime = exporter.runtime_summary(record)
        llvm = {"runtime": runtime, "llvm_version": "19.1.7", "static": True,
                "archives": ["/producer/o/llvm-cosmo/libLLVMCore.a"]}
        mesa = {"runtime": runtime, "mesa_version": "25.2.8", "llvm": llvm, "static": True,
                "external_vulkan_loader": False, "archive": "/producer/o/lavapipe/libmesa.a",
                "registration_archive": "/producer/o/lavapipe/libregistration.a",
                "link_args": ["/producer/o/lavapipe/libregistration.a", "-Wl,--whole-archive",
                              "/producer/o/lavapipe/libmesa.a", "-Wl,--no-whole-archive",
                              "/producer/o/llvm-cosmo/libLLVMCore.a", "-lm"]}
        build = {"rust": "nightly-2026-07-28", "wgpu_native": "v29.0.1.1",
                 "application_patches": {exporter.APPLICATION_PATCH: exporter.canonical_hash("patch")},
                 "prepared_source_sha256": exporter.canonical_hash("prepared"),
                 "rust_std_delivery": {"mode": "embedded-in-wgpu", "container_sha256": hashlib.sha256(archive_bytes()).hexdigest()}}
        raw = self.write("producer/BUILD.json", json.dumps({"runtime": runtime, **build}).encode())
        verified = {"record": record, "runtime": runtime, "build": build, "mesa": mesa, "llvm": llvm,
                    "shims": [Path("/producer/shim-test-x86_64.o")], "raw_records": {"webgpu/BUILD.json": raw},
                    "extra": {str(raw): exporter.digest_file(raw)}, "c_triple": "x86_64-unknown-linux-musl",
                    "rust_triple": "x86_64-unknown-linux-musl"}
        with patch.object(exporter, "git_archive_sha256", return_value=exporter.canonical_hash("synthetic archive")):
            pin = exporter.candidate_pin(self.root, verified)
        return stage, verified, pin

    def test_emits_complete_relative_contract_and_shared_rust_delivery(self):
        stage, verified, pin = self.assembly_fixture()
        data = exporter.assemble_handoff(stage, verified, pin)
        exporter.verify_export(stage, data, verified["record"])
        link = json.loads((stage / "dependencies/LINK.json").read_text())
        self.assertEqual(link["path_mode"], "export-root-relative")
        self.assertEqual(link["sdk_root"], "sdk")
        self.assertNotIn("sdk_archive_sha256", link)
        self.assertEqual(link["software_vulkan"], verified["mesa"])
        self.assertEqual(link["runtime_sources"], ["dependencies/runtime/vulkan_loader.c", "dependencies/runtime/win64_bridge.c"])
        self.assertEqual(data["components"]["rust_std"]["archives"], data["components"]["webgpu"]["archives"])
        self.assertEqual(data["runtime"]["provenance"]["patches_sha256"], exporter.canonical_hash([]))
        rust = json.loads((stage / data["components"]["rust_std"]["provenance"]).read_text())
        self.assertEqual(rust["archive_delivery"], verified["build"]["rust_std_delivery"])
        self.assertEqual(data["artifacts"]["sdk/x86_64-linux-cosmo/lib/libm.a"]["kind"], "data")
        self.assertIn("sdk/x86_64-linux-cosmo/lib/libm.a", data["toolchain"]["support_files"])
        self.assertEqual((stage / "provenance/webgpu/BUILD.json").read_bytes(),
                         verified["raw_records"]["webgpu/BUILD.json"].read_bytes())

    @unittest.skipUnless(os.environ.get("EASY_DIFFUSION_CONSUMER_DIR"), "Separate consumer checkout not selected")
    def test_emitted_handoff_matches_separate_consumer_format(self):
        # Synthetic bytes check the contract only, never compiler behavior.
        sys.path.insert(0, os.environ["EASY_DIFFUSION_CONSUMER_DIR"])
        from foundation_handoff import validate_handoff
        stage, verified, pin = self.assembly_fixture()
        data = exporter.assemble_handoff(stage, verified, pin)
        report = validate_handoff(data, stage, expected_repository=pin["repository"],
                                  expected_revision=pin["revision"], expected_architecture="x86_64")
        self.assertEqual(report["verification"], "static-identity-only")

    @unittest.skipUnless(os.environ.get("EASY_DIFFUSION_CONSUMER_DIR"), "Separate consumer checkout not selected")
    def test_synthetic_export_selects_with_candidate_pin_and_relocates(self):
        sys.path.insert(0, os.environ["EASY_DIFFUSION_CONSUMER_DIR"])
        from foundation_consumer import select_foundation
        from foundation_handoff import HandoffError
        stage, verified, pin = self.assembly_fixture()
        data = exporter.assemble_handoff(stage, verified, pin)
        source_link = (stage / "dependencies/LINK.json").read_bytes()
        def select(directory, selected_pin=pin):
            return select_foundation(directory / "HANDOFF.json", directory, directory / "sdk",
                                     directory / "dependencies/LINK.json", pin=selected_pin,
                                     application_patches=verified["build"]["application_patches"])
        selected = select(stage)
        self.assertEqual(pin["cosmocc_version"], "source-built")
        self.assertEqual(pin["paths"], ["."])
        self.assertEqual(selected["runtime"], verified["runtime"])
        self.assertEqual(selected["link_view"]["wgpu_archive"], str(stage / "dependencies/lib/wgpu/libwgpu_native.a"))
        self.assertEqual((stage / "dependencies/LINK.json").read_bytes(), source_link)
        wrong_pin = {**pin, "revision": "f" + pin["revision"][1:]}
        with self.assertRaises(HandoffError):
            select(stage, wrong_pin)
        relocated = self.root / "relocated"
        stage.rename(relocated)
        second = select(relocated)
        self.assertEqual(second["handoff_report"]["runtime_identity_sha256"], data["runtime_identity_sha256"])
        self.assertNotEqual(second["selection_identity_sha256"], selected["selection_identity_sha256"])
        self.assertEqual((relocated / "dependencies/LINK.json").read_bytes(), source_link)
        (relocated / "sdk/extra").write_bytes(b"not receipted")
        with self.assertRaises(HandoffError):
            select(relocated)

    def test_candidate_archive_hash_is_the_full_git_archive_stream(self):
        repository = self.root / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        self.write("repository/first.txt", b"tracked first")
        self.write("repository/nested/second.txt", b"tracked second")
        subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repository), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
                        "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"], check=True)
        revision = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
        raw = subprocess.check_output(["git", "-C", str(repository), "archive", "--format=tar", revision, "."])
        self.assertEqual(exporter.git_archive_sha256(repository, revision), hashlib.sha256(raw).hexdigest())

    def test_candidate_archive_ignores_inherited_git_environment(self):
        from source_runtime import git, git_bytes
        repository = self.root / "repository"
        other = self.root / "other-repository"
        repository.mkdir()
        other.mkdir()
        git_bytes(repository, "init", "-q")
        git_bytes(other, "init", "-q")
        self.write("repository/tracked.txt", b"selected committed bytes")
        git_bytes(repository, "add", ".")
        git_bytes(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "-c", "commit.gpgsign=false", "commit", "-qm", "archive fixture")
        revision = git(repository, "rev-parse", "HEAD")
        expected = hashlib.sha256(git_bytes(repository, "archive", "--format=tar", revision, ".")).hexdigest()
        global_config = self.write("global.gitconfig", b"[tar]\n\tumask = 0000\n")
        overrides = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                     "GIT_INDEX_FILE": str(self.root / "foreign-index"),
                     "GIT_CONFIG_GLOBAL": str(global_config), "GIT_CONFIG_COUNT": "1",
                     "GIT_CONFIG_KEY_0": "tar.umask", "GIT_CONFIG_VALUE_0": "0000"}
        with patch.dict(os.environ, overrides):
            self.assertEqual(exporter.git_archive_sha256(repository, revision), expected)

    def test_candidate_archive_ignores_local_overrides_but_keeps_committed_attributes(self):
        from source_runtime import git, git_bytes
        repository = self.root / "repository"
        repository.mkdir()
        git_bytes(repository, "init", "-q", "--template=")
        self.write("repository/.gitattributes", b"committed-ignore.txt export-ignore\ncommitted-subst.txt export-subst\n")
        self.write("repository/tracked.txt", b"selected committed bytes")
        self.write("repository/committed-ignore.txt", b"excluded by committed attributes")
        self.write("repository/committed-subst.txt", b"commit=$Format:%H$\n")
        git_bytes(repository, "add", ".")
        git_bytes(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "-c", "commit.gpgsign=false", "commit", "-qm", "archive attributes fixture")
        revision = git(repository, "rev-parse", "HEAD")
        raw = git_bytes(repository, "-c", "core.attributesFile=" + os.devnull,
                        "-c", "tar.umask=0002", "archive", "--format=tar", revision, ".")
        expected = hashlib.sha256(raw).hexdigest()
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            self.assertNotIn("committed-ignore.txt", archive.getnames())
            self.assertEqual(archive.extractfile("committed-subst.txt").read(), ("commit=" + revision + "\n").encode())
        self.assertEqual(exporter.git_archive_sha256(repository, revision), expected)
        info_attributes = self.write("repository/.git/info/attributes", b"tracked.txt export-ignore\ncommitted-ignore.txt -export-ignore\n")
        external_attributes = self.write("local.attributes", b"* export-ignore\n")
        git_bytes(repository, "config", "core.attributesFile", str(external_attributes))
        git_bytes(repository, "config", "tar.umask", "0000")
        config_path = repository / ".git/config"
        original_config, original_attributes = config_path.read_bytes(), info_attributes.read_bytes()
        self.assertNotEqual(hashlib.sha256(git_bytes(repository, "archive", "--format=tar", revision, ".")).hexdigest(), expected)
        self.assertEqual(exporter.git_archive_sha256(repository, revision), expected)
        self.assertEqual(config_path.read_bytes(), original_config)
        self.assertEqual(info_attributes.read_bytes(), original_attributes)

    def test_crlf_materialization_matches_real_export_source_gate(self):
        from source_runtime import git_bytes, materialize_source, tracked_source_inventory
        repository = self.root / "repository"
        repository.mkdir()
        git_bytes(repository, "init", "-q")
        git_bytes(repository, "config", "user.name", "Fixture")
        git_bytes(repository, "config", "user.email", "fixture@example.invalid")
        git_bytes(repository, "config", "core.autocrlf", "false")
        git_bytes(repository, "remote", "add", "origin", "https://github.com/example/foundation.git")
        self.write("repository/.gitattributes", b"*.bat text eol=crlf\n*.txt filter=blocked\n")
        self.write("repository/.gitignore", b"/o/\n")
        self.write("repository/fetch.bat", b"first line\nsecond line\n")
        self.write("repository/filter.txt", b"unfiltered committed bytes\n")
        abi_paths = ("libc/nt/struct/sid.h", "libc/dlopen/dladdr.c",
                     "libc/intrin/fds.h", "libc/proc/posix_spawn.c")
        for name in (*abi_paths, "tool/cosmocc/package.sh"):
            self.write("repository/" + name, ("synthetic source: " + name).encode())
        git_bytes(repository, "add", ".")
        git_bytes(repository, "-c", "commit.gpgsign=false", "commit", "-qm", "CRLF fixture")
        (repository / "fetch.bat").unlink()
        git_bytes(repository, "checkout-index", "--force", "--all")
        original = (repository / "fetch.bat").read_bytes()
        self.assertIn(b"\r\n", original)
        canonical = self.root / "canonical"
        materialized = materialize_source(repository, canonical)
        self.assertEqual((repository / "fetch.bat").read_bytes(), original)
        self.assertEqual((canonical / "fetch.bat").read_bytes(), b"first line\nsecond line\n")
        source = materialized["source"]
        files = tracked_source_inventory(canonical, source["revision"], source["tree"])
        inputs = {"schema_version": 1, "kind": "cosmopolitan-tracked-source-inputs",
                  "source": source, "files": files}
        inventory_path = self.write("sdk/SOURCE_INPUTS.json", json.dumps(inputs).encode())
        # Synthetic SDK source evidence exercises the actual export source gate;
        # this fixture is not a compiled SDK or a complete export invocation.
        record = {"source": source, "source_patches": [],
                  "source_tree_sha256": exporter.canonical_hash(files),
                  "source_inventory_record": {"path": "SOURCE_INPUTS.json",
                                              "sha256": exporter.digest_file(inventory_path)},
                  "source_abi_inputs": {name: files[name]["sha256"] for name in abi_paths},
                  "producer": {"entrypoint": "tool/cosmocc/package.sh",
                               "sha256": files["tool/cosmocc/package.sh"]["sha256"]}}
        self.write("sdk/SOURCE_RUNTIME.json", json.dumps(record).encode())
        with self.assertRaisesRegex(ValueError, "pinned Git blob"):
            exporter.verify_source(repository, self.root / "sdk", record)
        # Any clean-filter invocation would fail. Raw source verification must
        # not invoke one even though attributes name a configured filter.
        git_bytes(canonical, "config", "filter.blocked.required", "true")
        git_bytes(canonical, "config", "filter.blocked.clean", "false")
        self.assertEqual(exporter.verify_source(canonical, self.root / "sdk", record), inputs)
        self.write("canonical/o/generated.o", b"ignored output")
        self.assertEqual(exporter.verify_source(canonical, self.root / "sdk", record), inputs)
        extra = self.write("canonical/nonignored.log", b"unexpected input")
        with self.assertRaisesRegex(ValueError, "nonignored untracked"):
            exporter.verify_source(canonical, self.root / "sdk", record)
        extra.unlink()
        (canonical / "fetch.bat").write_bytes(b"changed raw bytes\n")
        with self.assertRaisesRegex(ValueError, "pinned Git blob"):
            exporter.verify_source(canonical, self.root / "sdk", record)

    def test_runtime_identity_changes_with_compiler_support_but_not_export_location(self):
        stage, verified, pin = self.assembly_fixture()
        data = exporter.assemble_handoff(stage, verified, pin)
        original = exporter.runtime_identity_sha256(data)
        changed = copy.deepcopy(data)
        support = changed["toolchain"]["support_files"][0]
        changed["artifacts"][support]["sha256"] = exporter.canonical_hash("changed")
        self.assertNotEqual(original, exporter.runtime_identity_sha256(changed))
        renamed = self.root / "relocated"
        stage.rename(renamed)
        exporter.verify_export(renamed, data, verified["record"])
        self.assertEqual(original, exporter.runtime_identity_sha256(data))


if __name__ == "__main__":
    unittest.main()
