"""Blindagem de ambiente: os testes não podem herdar o contexto do app host.

`ZCODE_PERSONAL_PROVIDER_CONFIG_FILE` e `ZCODE_DATA_BASE_DIR` são canais
internos pai→filho do worker (o engine os define por chamada para apontar ao
provider temporário e ao login isolado). Quando o pytest roda dentro do app
ZCode esses valores já existem no ambiente, têm prioridade sobre as fixtures
`ZCODE_WORKER_CONTEXT_*` e quebram a hermeticidade da suíte.
"""

from __future__ import annotations

import os
from pathlib import Path
import pytest


os.environ["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH", "")])

from scripts import make_candidate
make_candidate.main()

@pytest.fixture(autouse=True)
def _isolated_worker_environment(monkeypatch):
    for name in ("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", "ZCODE_DATA_BASE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ZCODE_WORKER_START_PLAN", "0")
    yield
