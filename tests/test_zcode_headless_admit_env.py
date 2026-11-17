"""Limite de memória da admissão headless configurável por env."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "bin" / "zcode_headless_admit.py"
DEFAULT_KIB = 4 * 1024 * 1024


def _load_helper():
    spec = importlib.util.spec_from_file_location("zcode_headless_admit_env", HELPER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


admit = _load_helper()


class AdmitMinMemEnvTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("ZCODE_ADMIT_MIN_MEM_MIB", None)

    def tearDown(self):
        os.environ.pop("ZCODE_ADMIT_MIN_MEM_MIB", None)
        if self._saved is not None:
            os.environ["ZCODE_ADMIT_MIN_MEM_MIB"] = self._saved

    def test_default_is_four_gib(self):
        self.assertEqual(admit.min_mem_kib(), DEFAULT_KIB)

    def test_env_uses_mib_to_kib(self):
        os.environ["ZCODE_ADMIT_MIN_MEM_MIB"] = "1024"
        self.assertEqual(admit.min_mem_kib(), 1024 * 1024)

    def test_invalid_env_falls_back_to_four_gib(self):
        for raw in ("abc", "10"):
            os.environ["ZCODE_ADMIT_MIN_MEM_MIB"] = raw
            self.assertEqual(admit.min_mem_kib(), DEFAULT_KIB)

    def test_memory_check_uses_env_limit(self):
        with tempfile.NamedTemporaryFile("w", encoding="ascii", delete=False) as handle:
            handle.write("MemAvailable: 2000000 kB\n")
            meminfo = Path(handle.name)
        self.addCleanup(meminfo.unlink)
        os.environ["ZCODE_ADMIT_MIN_MEM_MIB"] = "1024"
        self.assertTrue(admit.read_mem_available(meminfo))
        os.environ.pop("ZCODE_ADMIT_MIN_MEM_MIB", None)
        with self.assertRaises(admit.AdmissionError):
            admit.read_mem_available(meminfo)


if __name__ == "__main__":
    unittest.main()
