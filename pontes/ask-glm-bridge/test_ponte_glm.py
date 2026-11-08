"""Testes da ask-glm-bridge (ponte HTTP -> zcode-worker) com worker fake.

Sem rede externa nem ZCode real: a ponte sobe em 127.0.0.1:0 (ThreadingHTTPServer
da propria classe Handler do modulo) e o "zcode-worker" é um script fake em
tmp_path que grava argv/stdin/env em arquivo (indicado por FAKE_WORKER_OUT) e
responde JSON controlado por FAKE_WORKER_MODE (ok | error | timeout).
"""
import importlib.util
import json
import os
import stat
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

PONTE = Path(__file__).resolve().parent / "ask-glm-bridge.py"

FAKE_WORKER = r"""#!/usr/bin/env python3
import json, os, sys
dados = sys.stdin.read()
with open(os.environ["FAKE_WORKER_OUT"], "w") as f:
    json.dump({"argv": sys.argv[1:], "stdin": dados,
               "env_tem_provider":
                   "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE" in os.environ}, f)
modo = os.environ.get("FAKE_WORKER_MODE", "")
if modo == "error":
    print(json.dumps({"status": "error", "error": {"code": "cli_failed"}}))
    sys.exit(1)
if modo == "timeout":
    sys.exit(124)
print(json.dumps({"status": "ok", "sessionId": "sess_x", "response": "pong",
                  "usage": {"inputTokens": 1, "outputTokens": 1}}))
"""

_seq = 0


def carregar_ponte():
    """Importa ask-glm-bridge.py (hífen no nome) com o env já preparado —
    ZCODE_WORKER e DEFAULT_MODE são lidos na importação."""
    global _seq
    _seq += 1
    spec = importlib.util.spec_from_file_location(f"_ponte_teste_{_seq}", PONTE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ponte(tmp_path, monkeypatch):
    fake = tmp_path / "fake_zcode_worker.py"
    fake.write_text(FAKE_WORKER)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    saida = tmp_path / "fake_out.json"
    monkeypatch.setenv("ASK_GLM_ZCODE_WORKER", str(fake))
    monkeypatch.setenv("FAKE_WORKER_OUT", str(saida))
    monkeypatch.setenv("ASK_BRIDGE_MODE", "plan")
    # a ponte repassa os.environ ao worker: sem provider config no teste
    monkeypatch.delenv("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", raising=False)
    mod = carregar_ponte()
    # acumular() não pode sujar o estado real
    monkeypatch.setattr(mod, "STATE_DIR", str(tmp_path / "state"))
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    th = threading.Thread(target=servidor.serve_forever, daemon=True)
    th.start()
    try:
        yield {"mod": mod, "base": f"http://127.0.0.1:{servidor.server_port}",
               "out": saida, "cwd": tmp_path}
    finally:
        servidor.shutdown()
        servidor.server_close()
        th.join(timeout=5)


def post(base, path, corpo, headers=None):
    req = urllib.request.Request(
        base + path, data=json.dumps(corpo).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:  # 4xx/5xx também devolvem corpo
        return e.code, e.read()


def ler_fake(saida: Path) -> dict:
    return json.loads(saida.read_text())


def flags(argv):
    """Dicionário --flag -> valor (None para flags booleanas como --chat/--json)."""
    pares = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            tem_valor = i + 1 < len(argv) and not argv[i + 1].startswith("--")
            pares[argv[i]] = argv[i + 1] if tem_valor else None
            i += 2 if tem_valor else 1
        else:
            i += 1
    return pares


def chat_ok(cwd, extra=None, headers=None):
    corpo = {"model": "glm-5.3-flash",
             "messages": [{"role": "user", "content": "oi"}]}
    corpo.update(extra or {})
    padrao = {"X-ZCode-Mode": "plan", "X-Ask-Cwd": str(cwd)}
    padrao.update(headers or {})
    return corpo, padrao


def test_chat_plan_sem_stream(ponte):
    corpo, cab = chat_ok(ponte["cwd"])
    status, bruto = post(ponte["base"], "/v1/chat/completions", corpo, cab)
    assert status == 200
    dados = json.loads(bruto)
    assert dados["choices"][0]["message"]["content"] == "pong"
    assert dados["model"] == "glm-5.3-flash"
    reg = ler_fake(ponte["out"])
    pares = flags(reg["argv"])
    assert "--chat" in pares
    assert "--json" in pares
    assert pares["--model"] == "glm-5.3-flash"
    assert pares["--mode"] == "plan"
    assert pares["--cwd"] == os.path.realpath(ponte["cwd"])
    assert pares["--queue-timeout"] == "30"
    assert pares["--effort"] == "high"  # effort ausente -> high
    assert reg["stdin"] == "oi"
    assert reg["env_tem_provider"] is False


def test_chat_plan_stream(ponte):
    corpo, cab = chat_ok(ponte["cwd"], extra={"stream": True})
    status, bruto = post(ponte["base"], "/v1/chat/completions", corpo, cab)
    assert status == 200
    texto = bruto.decode()
    assert "pong" in texto
    assert "[DONE]" in texto


def test_chat_modo_edit_sem_chat_flag(ponte):
    corpo, cab = chat_ok(ponte["cwd"], headers={"X-ZCode-Mode": "edit"})
    status, bruto = post(ponte["base"], "/v1/chat/completions", corpo, cab)
    assert status == 200
    assert json.loads(bruto)["choices"][0]["message"]["content"] == "pong"
    reg = ler_fake(ponte["out"])
    assert "--chat" not in reg["argv"]
    assert flags(reg["argv"])["--mode"] == "edit"


def test_worker_rc1_da_502(ponte, monkeypatch):
    monkeypatch.setenv("FAKE_WORKER_MODE", "error")
    corpo, cab = chat_ok(ponte["cwd"])
    status, bruto = post(ponte["base"], "/v1/chat/completions", corpo, cab)
    assert status == 502
    assert "error" in json.loads(bruto)


def test_worker_rc124_da_504(ponte, monkeypatch):
    monkeypatch.setenv("FAKE_WORKER_MODE", "timeout")
    corpo, cab = chat_ok(ponte["cwd"])
    status, bruto = post(ponte["base"], "/v1/chat/completions", corpo, cab)
    assert status == 504
    assert "error" in json.loads(bruto)


@pytest.mark.parametrize("pedido,esperado", [
    ("xhigh", "max"),
    (None, "high"),
], ids=["xhigh-max", "ausente-high"])
def test_effort_mapeado(ponte, pedido, esperado):
    extra = {"reasoning_effort": pedido} if pedido else None
    corpo, cab = chat_ok(ponte["cwd"], extra=extra)
    status, bruto = post(ponte["base"], "/v1/chat/completions", corpo, cab)
    assert status == 200
    reg = ler_fake(ponte["out"])
    assert flags(reg["argv"])["--effort"] == esperado


def test_messages_sem_stream(ponte):
    status, bruto = post(ponte["base"], "/v1/messages", {
        "model": "glm-5.3-flash", "max_tokens": 100,
        "messages": [{"role": "user", "content": "oi"}],
    }, {"X-Ask-Cwd": str(ponte["cwd"])})
    assert status == 200
    dados = json.loads(bruto)
    assert dados["type"] == "message"
    assert dados["role"] == "assistant"
    textos = [b["text"] for b in dados["content"] if b.get("type") == "text"]
    assert textos == ["pong"]
    assert dados["usage"]["input_tokens"] == 1
    assert dados["usage"]["output_tokens"] == 1
