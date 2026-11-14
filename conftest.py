import os
from pathlib import Path
os.environ["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).parent / "adapters/zcode/src"), str(Path(__file__).parent / "adapters/zcode"), os.environ.get("PYTHONPATH", "")])
# Testes: fila do zcode-worker sem espera extra para --step-timeout continuar valendo nos fakes.
os.environ.setdefault("WORKER_ASK_ZCODE_QUEUE_TIMEOUT", "0.001")
# Testes nao podem olhar processos e vagas reais da maquina: sonda de slots desligada
# (testes de vaga fazem monkeypatch em availability.slots).
os.environ.setdefault("WORKER_ASK_SLOT_PROBE", "0")
# Testes usam a ordem padrao da cadeia, nunca a salva pelo painel.
os.environ["WORKER_ASK_ORDEM"] = "/nonexistent/ordem.json"
