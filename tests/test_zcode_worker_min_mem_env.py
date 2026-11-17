"""Mínimo de memória do zcode_worker ajustável por ZCODE_ADMIT_MIN_MEM_MIB (container 2 GB)."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters" / "zcode" / "src"))
from zcode_worker import base  # noqa: E402

KEY = "ZCODE_ADMIT_MIN_MEM_MIB"


class MinMemAvailableEnvTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(KEY, None)

    def tearDown(self):
        os.environ.pop(KEY, None)
        if self._saved is not None:
            os.environ[KEY] = self._saved

    def test_sem_env_mantem_4gib(self):
        self.assertEqual(base.min_mem_available(), 4 * 1024 ** 3)

    def test_env_valida_ajusta(self):
        os.environ[KEY] = "1024"
        self.assertEqual(base.min_mem_available(), 1024 ** 3)

    def test_env_invalida_ou_baixa_cai_no_padrao(self):
        for raw in ("", "abc", "-1", "100", "1.5"):
            os.environ[KEY] = raw
            self.assertEqual(base.min_mem_available(), 4 * 1024 ** 3, raw)


if __name__ == "__main__":
    unittest.main()
