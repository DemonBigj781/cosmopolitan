"""Validate the dual-architecture Lua wrapper build recipe."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "toolchain/prepare-lua.sh"


class PrepareLuaTest(unittest.TestCase):
    def test_wrapper_uses_repository_build_for_each_architecture(self):
        script = SCRIPT.read_text()

        self.assertRegex(
            script,
            re.compile(
                r'"\$COSMO_MAKE" -s -j1 MODE= '
                r'\$LUA_DEPS o//toolchain/lua_entry\.o'
            ),
        )
        self.assertRegex(
            script,
            re.compile(
                r'"\$COSMO_MAKE" -s -j1 MODE=aarch64 '
                r'\$ARM_LUA_DEPS o/aarch64/toolchain/lua_entry\.o'
            ),
        )
        self.assertNotIn('"$COSMOCC"', script)
        self.assertIn(
            "cp o/aarch64/toolchain/lua_entry.o "
            "o/toolchain/.aarch64/lua_entry.o",
            script,
        )


if __name__ == "__main__":
    unittest.main()
