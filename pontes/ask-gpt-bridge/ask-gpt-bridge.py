#!/usr/bin/env python3
"""ask-gpt-bridge — ponte HTTP local (OpenAI-compatible) -> CLI do Codex oficial (ChatGPT nativo).

Faz com que o ZCode (ou qualquer cliente OpenAI-compatible) use o ChatGPT atraves do
Codex instalado (login ChatGPT do proprio usuario), sem chave de API armazenada.

  GET  /v1/models                  -> catalogo GPT (lido do proprio binario do codex)
  POST /v1/chat/completions        -> roda `codex exec --json --ephemeral -s read-only`
                                      (so leitura; sandboxes: read-only|workspace-write
                                      via ASK_GPT_BRIDGE_SANDBOX)
  Header X-Ask-Cwd: <dir>          -> cwd da chamada (padrao $HOME)
  reasoning_effort                 -> (low|medium|high|xhigh|max|ultra) vira `-c model_reasoning_effort=...`; fora da lista = 400

Streaming real: com "stream": true os headers SSE saem ANTES de rodar o codex e o
progresso das ferramentas (▶ inicio, ✔/✖ fim) vai ao vivo em delta.reasoning_content
(10 s sem chunk = comentario SSE ": ping"), seguido do chunk de conteudo, chunk final
finish_reason=stop e [DONE]. Erro depois dos headers vira chunk de conteudo
"[erro da ponte: ...]" + fim — nunca um segundo status HTTP. O prompt entra por stdin
(`codex exec ... -`), sem o teto de 128 KB do argv; o corpo da requisicao aceita no
maximo 4 MiB (Content-Length invalido/ausente/negativo/maior = 400 antes da leitura).
Tool calls NAO sao devolvidas ao cliente: o codex usa as proprias ferramentas no cwd
e a ponte so entrega o texto final (arquivo -o, autoritativo; vazio -> ultima
agent_message do stream JSON). Tokens de usage vem do turn.completed. Erros: 400
entrada invalida, 504 timeout, 502 falha do codex — mensagem curta, sem stderr
(no journal vai so o tamanho de stderr); saida de comando e prompt nunca sao emitidos.

Roteamento NATIVO garantido: roda com CODEX_HOME dedicado
(~/.config/ask-gpt-bridge/codex-home) que ignora o openai_base_url/model_catalog_json
que o OpenCodex injeta em ~/.codex/config.toml. O auth.json e SYMLINK ao
~/.codex/auth.json — nenhuma credencial e copiada; re-login do usuario repica aqui.

Servico: systemd user `ask-gpt-bridge.service`, porta 10112 (so loopback).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ask_bridge_stream import FalhaStreaming, executar_limitado

STATE_DIR = os.path.expanduser("~/.local/state/ask-bridges")
_lock = threading.Lock()


def acumular(provedor: str, in_tok: int, out_tok: int, custo: float = 0.0):
    """Contador diario de consumo via ponte (fonte do ask-cotas)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    caminho = os.path.join(STATE_DIR, f"uso-{provedor}-{time.strftime('%Y-%m-%d')}.json")
    with _lock:
        try:
            d = json.load(open(caminho))
        except Exception:  # noqa: BLE001 — arquivo novo/corrompido recomeca do zero
            d = {"calls": 0, "in": 0, "out": 0, "custo": 0.0}
        d["calls"] += 1
        d["in"] += int(in_tok or 0)
        d["out"] += int(out_tok or 0)
        d["custo"] = round(d.get("custo", 0.0) + custo, 6)
        tmp = caminho + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, caminho)


PORT = int(os.environ.get("ASK_GPT_BRIDGE_PORT", "10112"))
CODEX_BIN = os.environ.get("ASK_GPT_BIN", "codex")
SANDBOX = os.environ.get("ASK_GPT_BRIDGE_SANDBOX", "read-only")
TIMEOUT = int(os.environ.get("ASK_GPT_BRIDGE_TIMEOUT", "1200"))
CODEX_HOME = os.environ.get("ASK_GPT_CODEX_HOME",
                            os.path.expanduser("~/.config/ask-gpt-bridge/codex-home"))
AUTH_FONTE = os.path.expanduser("~/.codex/auth.json")
MODELOS_FALLBACK = ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-luna"]
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
MAX_BODY = 4 * 1024 * 1024

_cache_bundle = {"mtime": None, "modelos": list(MODELOS_FALLBACK)}


class ErroEntrada(Exception):
    """Requisicao invalida -> HTTP 400."""


def preparar_codex_home():
    """CODEX_HOME nativo: config limpa (sem injecao do ocx) + auth.json por symlink."""
    os.makedirs(CODEX_HOME, exist_ok=True)
    link = os.path.join(CODEX_HOME, "auth.json")
    if os.path.exists(AUTH_FONTE) and not os.path.islink(link):
        try:
            os.symlink(AUTH_FONTE, link)
        except OSError:
            pass
    config = os.path.join(CODEX_HOME, "config.toml")
    if not os.path.exists(config):
        with open(config, "w") as f:
            f.write('# ask-gpt-bridge: codex NATIVO (plano ChatGPT). Sem base_url do ocx.\n'
                    'model_reasoning_effort = "medium"\n')


def _resolver_binario() -> str:
    """Binario real do codex: o launcher e um codex.js (npm); o catalogo vive no binario
    rust vendorizado ao lado (node_modules/@openai/codex-linux-x64/vendor/*/bin/codex)."""
    binario = CODEX_BIN
    if "/" not in binario:
        from shutil import which
        binario = which(CODEX_BIN) or CODEX_BIN
    real = os.path.realpath(binario)
    if os.path.isfile(real) and os.stat(real).st_size > 10 * 1024 * 1024:
        return real
    # launcher pequeno: procura o binario rust no pacote npm vizinho
    raiz = os.path.dirname(os.path.dirname(real))  # .../@openai/codex
    import glob
    candidatos = glob.glob(os.path.join(
        raiz, "node_modules", "@openai", "codex-linux-*", "vendor", "*", "bin", "codex"))
    for c in sorted(candidatos, reverse=True):
        if os.access(c, os.X_OK):
            return c
    return real


def descobrir_modelos() -> list:
    """Le o catalogo do proprio binario do codex (fonte de verdade da OpenAI).

    O binario rust embute o template JSON de ModelsResponse com chaves "slug" —
    mantem so os ids versionados (gpt-<numero>...), descarta internos
    (codex-auto-review, variantes daybreak). Re-escaneia so quando o mtime muda
    (update do cliente = lista nova sozinha). Falhou? Usa o fallback.
    """
    try:
        real = _resolver_binario()
        mtime = os.stat(real).st_mtime
        if _cache_bundle["mtime"] != mtime:
            texto = open(real, "rb").read().decode("utf-8", "ignore")
            slugs = [s for s in re.findall(r'"slug":\s*"([^"]+)"', texto)
                     if re.match(r"^gpt-\d", s)]
            # versao mais nova primeiro; variante por nome como desempate estavel
            def chave(s):
                m = re.match(r"gpt-(\d+)(?:\.(\d+))?", s)
                return (int(m.group(1)), int(m.group(2) or 0), s)
            slugs.sort(key=chave, reverse=True)
            if slugs:
                _cache_bundle["modelos"] = slugs
            _cache_bundle["mtime"] = mtime
    except OSError:
        pass
    return list(_cache_bundle["modelos"])


id_seq = 0


def novo_id() -> str:
    global id_seq
    id_seq += 1
    return f"ask-gpt-bridge-{int(time.time())}-{id_seq}"


def prompt_das_messages(messages) -> str:
    partes = []
    for m in messages or []:
        papel = m.get("role", "user")
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        if papel == "system":
            partes.append(f"[instrucoes] {c}")
        elif papel == "assistant":
            partes.append(f"[resposta anterior] {c}")
        else:
            partes.append(str(c))
    return "\n\n".join(partes).strip()


def comando_curto(command: str) -> str:
    """Comando do codex numa linha so: sem prefixo bash -lc/aspas externas, 100 chars."""
    t = (command or "").strip()
    if t.startswith("/bin/bash -lc "):
        t = t[len("/bin/bash -lc "):].strip()
        if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
            t = t[1:-1]
    return " ".join(t.split())[:100]


def rodar_codex(prompt: str, cwd: str, modelo: str, effort=None, progresso=None):
    """Roda o Codex oficial com ChatGPT nativo; devolve (conteudo, in_tokens, out_tokens).

    `codex exec --json ... -`: prompt por stdin (sem teto de argv), eventos JSONL no
    stdout. Com progresso, comandos viram linhas ▶/✔/✖ e mensagens intermediarias
    viram progresso cortado em 500 chars — a ULTIMA agent_message e a resposta e
    nunca vira progresso. Resposta final = arquivo -o (autoritativo); vazio ->
    ultima agent_message. aggregated_output e o prompt nunca sao emitidos.
    """
    preparar_codex_home()
    env = dict(os.environ, CODEX_HOME=CODEX_HOME)
    fd, caminho_out = tempfile.mkstemp(prefix="ask-gpt-out-", suffix=".txt")
    os.close(fd)
    cmd = [CODEX_BIN, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
           "-s", SANDBOX, "-C", cwd]
    if modelo:
        cmd += ["-m", modelo]
    if effort is not None:  # valor ja validado pela allowlist EFFORTS
        cmd += ["-c", f"model_reasoning_effort={effort}"]
    cmd += ["-o", caminho_out, "-"]
    ultima_msg = None   # ultima agent_message: resposta de reserva se o -o vier vazio
    pendente = None     # fala intermediaria ainda nao emitida como progresso
    in_t = out_t = 0
    erro_final = None

    def por_linha(linha: str):
        nonlocal ultima_msg, pendente, in_t, out_t, erro_final
        try:
            ev = json.loads(linha)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        tipo = ev.get("type")
        item = ev.get("item")
        if tipo == "item.started":
            if not isinstance(item, dict):
                return
            if pendente and progresso is not None:  # fala intermediaria antes da acao
                progresso(pendente[:500])
            pendente = None
            it = item.get("type")
            if progresso is None:
                return
            if it == "command_execution":
                progresso("▶ " + comando_curto(str(item.get("command") or "")))
            elif it not in ("agent_message", "reasoning"):
                progresso("▶ " + str(it))
        elif tipo == "item.completed":
            if not isinstance(item, dict):
                return
            it = item.get("type")
            if it == "command_execution":
                if progresso is not None:
                    curto = comando_curto(str(item.get("command") or ""))
                    rc_item = item.get("exit_code")
                    if rc_item == 0:
                        progresso("✔ " + curto)
                    else:
                        progresso(f"✖ {curto} (exit {rc_item})")
            elif it == "agent_message":
                texto = item.get("text")
                if not isinstance(texto, str):
                    texto = ""
                # a mensagem anterior ainda pendente vira progresso; a ultima e a resposta
                if pendente and progresso is not None:
                    progresso(pendente[:500])
                pendente = texto
                ultima_msg = texto
        elif tipo == "turn.completed":
            usage = ev.get("usage") or {}
            in_t = usage.get("input_tokens") if isinstance(usage.get("input_tokens"), int) else 0
            out_t = usage.get("output_tokens") if isinstance(usage.get("output_tokens"), int) else 0
        elif tipo in ("turn.failed", "error"):
            msg = ev.get("message")
            if not isinstance(msg, str) or not msg:
                err = ev.get("error")
                msg = err.get("message") if isinstance(err, dict) else None
            if isinstance(msg, str) and msg:
                erro_final = msg[:300]

    try:
        p = executar_limitado(cmd, entrada=prompt, timeout=TIMEOUT, env=env, cwd=cwd,
                              por_linha=por_linha, pulso=progresso,
                              capturar_stdout=False)
        conteudo = ""
        try:
            conteudo = open(caminho_out).read().strip()
        except OSError:
            pass
    finally:
        try:
            os.unlink(caminho_out)
        except OSError:
            pass
    if not conteudo and ultima_msg:
        conteudo = ultima_msg.strip()
    if p.returncode != 0 or not conteudo:
        # journal so com tamanhos: stderr/conteudo podem carregar dados do usuario
        print(f"[gpt-bridge] codex rc={p.returncode} stderr={len(p.stderr or '')}B",
              flush=True)
        msg = f"codex falhou (rc={p.returncode})"
        if erro_final:
            msg += f": {erro_final}"
        raise RuntimeError(msg)
    return conteudo, in_t, out_t


def resposta_chat(conteudo: str, modelo: str) -> dict:
    return {
        "id": novo_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": modelo,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": conteudo}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            print("[gpt-bridge] cliente desconectou antes da resposta", flush=True)

    def _ler_corpo(self) -> dict:
        """Le e valida o corpo JSON (dict); Content-Length/corpo invalido = 400."""
        bruto = self.headers.get("Content-Length")
        try:
            tamanho = int(bruto) if bruto is not None else -1
        except ValueError:
            raise ErroEntrada("corpo invalido ou acima de 4 MiB") from None
        if tamanho < 0 or tamanho > MAX_BODY:
            raise ErroEntrada("corpo invalido ou acima de 4 MiB")
        try:
            corpo = json.loads(self.rfile.read(tamanho) or b"{}")
        except ValueError:
            raise ErroEntrada("corpo JSON invalido") from None
        if not isinstance(corpo, dict):
            raise ErroEntrada("corpo deve ser um objeto JSON")
        return corpo

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            agora = int(time.time())
            self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": agora, "owned_by": "gpt-bridge"}
                for m in descobrir_modelos()]})
        elif self.path.rstrip("/") == "/healthz":
            self._json(200, {"ok": True, "sandbox": SANDBOX, "bin": CODEX_BIN,
                             "codex_home": CODEX_HOME, "live_streaming": True,
                             "native_tool_calls": False, "max_body": MAX_BODY})
        else:
            self._json(404, {"error": {"message": "rota desconhecida"}})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._json(404, {"error": {"message": "rota desconhecida"}})
            return
        modelo = ""
        stream_ativo = False
        try:
            corpo = self._ler_corpo()
            effort = corpo.get("reasoning_effort")
            if effort is not None:
                if not isinstance(effort, str) or effort.strip().lower() not in EFFORTS:
                    raise ErroEntrada("reasoning_effort invalido; permitidos: " + ", ".join(EFFORTS))
                effort = effort.strip().lower()
            modelo = corpo.get("model", "")
            if modelo not in descobrir_modelos():
                modelo = ""  # desconhecido: codex usa o padrao do plano
            prompt = prompt_das_messages(corpo.get("messages"))
            if not prompt:
                raise ErroEntrada("sem prompt")
            cwd = self.headers.get("X-Ask-Cwd") or os.path.expanduser("~")
            if corpo.get("stream"):
                stream_ativo = True
                self._abrir_sse(modelo)

                # parser chama com a linha; o pulso do helper chama sem argumentos
                # (linha "") e vira keepalive (: ping apos 10 s sem chunk)
                def progresso(linha: str = ""):
                    if linha:
                        self._delta(modelo, {"reasoning_content": linha + "\n"})
                    else:
                        self._ping()

                conteudo, in_t, out_t = rodar_codex(prompt, cwd, modelo, effort=effort,
                                                    progresso=progresso)
                acumular("gpt", in_t, out_t)
                self._delta(modelo, {"content": conteudo})
                self._fim_stream(modelo)
            else:
                conteudo, in_t, out_t = rodar_codex(prompt, cwd, modelo, effort=effort)
                acumular("gpt", in_t, out_t)
                self._json(200, resposta_chat(conteudo, modelo))
        except ErroEntrada as e:
            self._json(400, {"error": {"message": str(e)}})
        except subprocess.TimeoutExpired:
            if stream_ativo:
                self._erro_no_stream(modelo, f"codex excedeu {TIMEOUT}s")
            else:
                self._json(504, {"error": {"message": f"codex excedeu {TIMEOUT}s"}})
        except FalhaStreaming as e:
            if stream_ativo:
                self._erro_no_stream(modelo, str(e)[:300])
            else:
                self._json(502, {"error": {"message": str(e)[:800]}})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # desconexao ou cliente parado durante o stream: o callback ja matou o codex (helper)
            print("[gpt-bridge] cliente desconectou durante o stream", flush=True)
        except Exception as e:  # noqa: BLE001 — erro curto ao chamador, sem stack
            if stream_ativo:
                self._erro_no_stream(modelo, str(e)[:300])
            else:
                self._json(502, {"error": {"message": str(e)[:800]}})

    def _abrir_sse(self, modelo: str):
        """Status 200 + headers SSE antes de rodar o codex + 1o delta (so role)."""
        self._stream_id = novo_id()
        self._wlock = threading.Lock()  # lock local da requisicao
        self._ultimo_envio = time.monotonic()
        # sem prazo, cliente parado bloqueia o write e o timeout do codex nunca dispara
        self.connection.settimeout(30)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self._delta(modelo, {"role": "assistant"})

    def _escrever_sse(self, dados: bytes):
        """Escrita SSE unica (sob lock); BrokenPipe/Reset sobem p/ matar o codex."""
        with self._wlock:
            self.wfile.write(dados)
            self.wfile.flush()
            self._ultimo_envio = time.monotonic()

    def _delta(self, modelo: str, delta: dict):
        chunk = {"id": self._stream_id, "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": modelo,
                 "choices": [{"index": 0, "finish_reason": None, "delta": delta}]}
        self._escrever_sse(f"data: {json.dumps(chunk)}\n\n".encode())

    def _ping(self):
        """Keepalive: comentario SSE se passaram 10 s sem nenhum chunk."""
        if time.monotonic() - self._ultimo_envio >= 10:
            self._escrever_sse(b": ping\n\n")

    def _fim_stream(self, modelo: str):
        """Chunk final (finish_reason stop) + [DONE]."""
        fim = {"id": self._stream_id, "object": "chat.completion.chunk",
               "created": int(time.time()), "model": modelo,
               "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]}
        self._escrever_sse(f"data: {json.dumps(fim)}\n\n".encode())
        self._escrever_sse(b"data: [DONE]\n\n")

    def _erro_no_stream(self, modelo: str, msg: str):
        """Erro com headers ja enviados: content [erro da ponte] + fim; nunca status."""
        try:
            self._delta(modelo, {"content": f"[erro da ponte: {msg}]"})
            self._fim_stream(modelo)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # cliente sumiu: ja logado no ponto da desconexao

    def log_message(self, fmt, *args):  # log enxuto no journal
        print(f"[gpt-bridge] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    preparar_codex_home()
    print(f"[gpt-bridge] ouvindo 127.0.0.1:{PORT} sandbox={SANDBOX} bin={CODEX_BIN} "
          f"codex_home={CODEX_HOME}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
