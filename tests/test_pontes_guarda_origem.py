"""Guarda de origem das pontes HTTP: POST exige Host loopback da porta do
servico, recusa cabecalho Origin (navegador) e exige Content-Type
application/json — CSRF/DNS rebinding nao podem rodar o CLI do usuario.

As pontes sobem em 127.0.0.1:0 com o Handler do proprio modulo; o CLI de cada
ponte (rodar_codex/rodar_claude/rodar_zcode) e trocado por fakes, sem rede
externa nem credenciais.
"""
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
PONTES = {
    "gpt": RAIZ / "pontes/ask-gpt-bridge/ask-gpt-bridge.py",
    "claude": RAIZ / "pontes/ask-claude-bridge/ask-claude-bridge.py",
    "glm": RAIZ / "pontes/ask-glm-bridge/ask-glm-bridge.py",
}
CORPO_CHAT = {
    "gpt": {"model": "gpt-6-astra", "messages": [{"role": "user", "content": "oi"}]},
    "claude": {"model": "claude-haiku-5-5",
               "messages": [{"role": "user", "content": "oi"}]},
    "glm": {"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "oi"}]},
}
_seq = 0


def carregar_ponte(nome: str):
    """Importa o modulo da ponte com os modulos comuns (ask_bridge_*) no caminho."""
    global _seq
    _seq += 1
    for relativo in ("pontes/comum", "pontes/ask-glm-bridge"):
        caminho = str(RAIZ / relativo)
        if caminho not in sys.path:
            sys.path.insert(0, caminho)
    spec = importlib.util.spec_from_file_location(f"_ponte_guarda_{nome}_{_seq}", PONTES[nome])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=sorted(PONTES))
def ponte(request, tmp_path, monkeypatch):
    mod = carregar_ponte(request.param)
    monkeypatch.setattr(mod, "STATE_DIR", str(tmp_path / "state"))  # acumular fora do HOME real
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"nome": request.param, "mod": mod,
               "base": f"http://127.0.0.1:{servidor.server_port}"}
    finally:
        servidor.shutdown()
        servidor.server_close()
        thread.join(timeout=5)


def post(base, caminho, corpo, headers):
    req = urllib.request.Request(
        base + caminho, data=json.dumps(corpo).encode(),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:  # 4xx/5xx tambem devolvem corpo
        return e.code, e.read()


@pytest.fixture
def cli_proibido(ponte, monkeypatch):
    """Substitui o CLI da ponte por um que falha o teste se rodar."""
    nome = ponte["nome"]
    def explosao(*_args, **_kwargs):
        raise AssertionError("CLI rodou sem passar pela guarda de origem")
    monkeypatch.setattr(ponte["mod"],
                        {"gpt": "rodar_codex", "claude": "rodar_claude",
                         "glm": "rodar_zcode"}[nome], explosao)


def test_post_host_de_outra_origem_recusado(ponte, cli_proibido):
    status, bruto = post(ponte["base"], "/v1/chat/completions", CORPO_CHAT[ponte["nome"]],
                         {"Host": "rebind.exemplo:10110"})
    assert status == 400
    assert "Host" in json.loads(bruto)["error"]["message"]


def test_post_com_origin_de_navegador_recusado(ponte, cli_proibido):
    status, bruto = post(ponte["base"], "/v1/chat/completions", CORPO_CHAT[ponte["nome"]],
                         {"Origin": "http://malicioso.exemplo"})
    assert status == 400
    assert "Origin" in json.loads(bruto)["error"]["message"]


def test_post_sem_application_json_recusado(ponte, cli_proibido):
    status, bruto = post(ponte["base"], "/v1/chat/completions", CORPO_CHAT[ponte["nome"]],
                         {"Content-Type": "text/plain"})
    assert status == 400
    assert "Content-Type" in json.loads(bruto)["error"]["message"]


def test_post_bom_cliente_local_passa(ponte, monkeypatch):
    nome, mod = ponte["nome"], ponte["mod"]
    if nome == "gpt":
        monkeypatch.setattr(mod, "rodar_codex", lambda *a, **k: ("pong", 1, 2))
    elif nome == "claude":
        def claude_fake(_prompt, _cwd, _modo, modelo, **_k):
            return {"result": "pong", "is_error": False, "subtype": "success",
                    "modelUsage": {modelo: 1}, "usage": {"input_tokens": 1,
                                                         "output_tokens": 2},
                    "total_cost_usd": 0.0}
        monkeypatch.setattr(mod, "rodar_claude", claude_fake)
    else:
        resposta = {"status": "ok", "sessionId": "s1", "response": "pong",
                    "usage": {"inputTokens": 1, "outputTokens": 2}}
        monkeypatch.setattr(mod, "rodar_zcode", lambda *a, **k: (resposta, "s1"))
    status, bruto = post(ponte["base"], "/v1/chat/completions", CORPO_CHAT[nome], {})
    assert status == 200
    assert json.loads(bruto)["choices"][0]["message"]["content"] == "pong"


@pytest.mark.parametrize("nome", ["claude", "glm"])
def test_rota_messages_tambem_guardada(nome, tmp_path, monkeypatch):
    """POST /v1/messages (formato Anthropic) passa pela mesma guarda."""
    mod = carregar_ponte(nome)
    monkeypatch.setattr(mod, "STATE_DIR", str(tmp_path / "state"))
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    try:
        corpo = {"model": CORPO_CHAT[nome]["model"], "max_tokens": 10,
                 "messages": [{"role": "user", "content": "oi"}]}
        status, _bruto = post(f"http://127.0.0.1:{servidor.server_port}",
                              "/v1/messages", corpo, {"Origin": "http://malicioso.exemplo"})
        assert status == 400
    finally:
        servidor.shutdown()
        servidor.server_close()
        thread.join(timeout=5)
