"""Lock the recursive APE command and provenance contract."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
DISPATCHER = ROOT / "toolchain/ape_toolchain.c"
SELF_BUILD_SOURCE = ROOT / "toolchain/self_build.c"


class RecursiveSelfBuildTest(unittest.TestCase):
    def test_dispatcher_exposes_self_build(self):
        source = DISPATCHER.read_text()

        self.assertIn('!strcmp(argv[1], "self-build")', source)
        self.assertIn("self_build_main(argc, argv)", source)

    def test_builder_copies_parent_resources_into_each_generation(self):
        source = SELF_BUILD_SOURCE.read_text()

        self.assertIn('"bin/zipcopy"', source)
        self.assertIn("SDK_ALL", source)
        self.assertIn("self-build/manifest.json", source)

    def test_generation_provenance_is_sidecar_only(self):
        source = SELF_BUILD_SOURCE.read_text()

        for field in (
            '"schema_version"',
            '"generation"',
            '"parent_path"',
            '"parent_sha256"',
            '"artifact_sha256"',
            '"artifact_size"',
            '"self_build_manifest_sha256"',
            '"sdk_metadata_sha256"',
        ):
            with self.subTest(field=field):
                self.assertIn(field, source)
        self.assertIn(".provenance.json", source)


if __name__ == "__main__":
    unittest.main()
