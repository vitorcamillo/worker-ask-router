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
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

PONTE = Path(__file__).resolve().parent / "ask-glm-bridge.py"
RAIZ = PONTE.parents[2]  # repo: bin/zcode-worker + adapters/zcode/src

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


def test_messages_stream(ponte):
    status, bruto = post(ponte["base"], "/v1/messages", {
        "model": "glm-5.3-flash", "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "oi"}],
    }, {"X-Ask-Cwd": str(ponte["cwd"])})
    assert status == 200
    texto = bruto.decode()
    assert "message_start" in texto
    assert "pong" in texto  # text_delta com a resposta final
    assert "message_delta" in texto
    assert "message_stop" in texto


def test_prompt_acima_do_limite_da_400_sem_chamar_worker(ponte):
    limite = ponte["mod"].MAX_PROMPT_BYTES
    corpo = {"model": "glm-5.3-flash",
             "messages": [{"role": "user", "content": "a" * (limite + 1)}]}
    status, bruto = post(ponte["base"], "/v1/chat/completions", corpo,
                         {"X-ZCode-Mode": "plan", "X-Ask-Cwd": str(ponte["cwd"])})
    assert status == 400
    assert "excede" in json.loads(bruto)["error"]["message"]
    assert not ponte["out"].exists()  # worker nem chegou a rodar


def test_effort_invalido_com_stream_da_400_antes_do_stream(ponte):
    """effort invalido tem que virar 400 mesmo com stream: true (o 200 SSE so
    sai depois de toda a validacao de entrada)."""
    corpo, cab = chat_ok(ponte["cwd"], extra={"stream": True,
                                              "reasoning_effort": 7})
    status, bruto = post(ponte["base"], "/v1/chat/completions", corpo, cab)
    assert status == 400
    assert "effort" in json.loads(bruto)["error"]["message"]
    assert not ponte["out"].exists()


def test_resolver_modo_header_contexto_e_invalido(ponte):
    mod = ponte["mod"]
    user = [{"role": "user", "content": "oi"}]
    assert mod.resolver_modo({"X-ZCode-Mode": "edit"}, user) == "edit"
    assert mod.resolver_modo({}, user) == "plan"  # default do fixture
    with pytest.raises(mod.ErroEntrada):
        mod.resolver_modo({"X-ZCode-Mode": "yolo"}, user)
    # ultimo "# Collaboration Mode: plan" no controle vence o header
    plan_ctx = [{"role": "system", "content":
                 "<collaboration_mode>\n# Collaboration Mode: plan\n"
                 "</collaboration_mode>"}]
    assert mod.resolver_modo({"X-ZCode-Mode": "edit"}, plan_ctx) == "plan"
    # palavra que nao e plan nao ativa; vale o header
    edit_ctx = [{"role": "system", "content":
                 "<collaboration_mode>\n# Collaboration Mode: edit\n"
                 "</collaboration_mode>"}]
    assert mod.resolver_modo({"X-ZCode-Mode": "edit"}, edit_ctx) == "edit"


def test_resolver_modo_lembrete_plan_do_app_zcode(ponte):
    mod = ponte["mod"]
    msgs = [{"role": "user", "content":
             "<system-reminder>\nPlan mode is active. Nao edite arquivos."
             "</system-reminder>"}]
    assert mod.resolver_modo({"X-ZCode-Mode": "edit"}, msgs) == "plan"


def test_resolver_cwd_header_contexto_e_erros(ponte, tmp_path):
    mod = ponte["mod"]
    alvo = tmp_path / "alvo"
    alvo.mkdir()
    assert mod.resolver_cwd({"X-Ask-Cwd": str(alvo)}, [], "edit") == \
        os.path.realpath(alvo)
    with pytest.raises(mod.ErroEntrada):  # header invalido: 400 sem fallback
        mod.resolver_cwd({"X-Ask-Cwd": "/caminho/que/nao/existe"}, [], "plan")
    ctx = [{"role": "system", "content":
            f"<environment_context>\n<cwd>{alvo}</cwd>\n</environment_context>"}]
    assert mod.resolver_cwd({}, ctx, "edit") == os.path.realpath(alvo)
    ctx_ruim = [{"role": "system", "content":
                 "<environment_context>\n<cwd>/caminho/que/nao/existe</cwd>\n"
                 "</environment_context>"}]
    with pytest.raises(mod.ErroEntrada):  # contexto invalido: 400 sem fallback
        mod.resolver_cwd({}, ctx_ruim, "plan")
    assert mod.resolver_cwd({}, [], "plan") == os.path.expanduser("~")
    with pytest.raises(mod.ErroEntrada):  # edit exige cwd explicito
        mod.resolver_cwd({}, [], "edit")


def test_contrato_worker_real_aceita_as_flags_da_ponte():
    """Contrato com o worker real (nao o fake): a ponte passa --json (sempre) e
    --chat (modo plan); o --help do bin/zcode-worker, resolvendo o adapter do
    repo como na chamada real, precisa declarar as duas flags."""
    worker = RAIZ / "bin" / "zcode-worker"
    if not worker.exists():
        pytest.fail(f"worker real ausente: {worker}")
    env = dict(os.environ)
    env["ASK_PRESENTATION_ACTIVE"] = "1"  # a ponte sempre ativa este env
    r = subprocess.run([sys.executable, str(worker), "--help"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr[-400:]
    assert "--json" in r.stdout
    assert "--chat" in r.stdout
