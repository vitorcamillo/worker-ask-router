#!/usr/bin/env python3
"""ask-glm-bridge — ponte HTTP local (OpenAI/Anthropic-compatible) -> zcode-worker.

Faz com que o OpenCodex (ou qualquer cliente OpenAI/Anthropic-compatible) use o GLM
atraves do ZCode. Princípio do dono: a ponte NAO escolhe provider nem gerencia plano —
quem decide a rotação Start Plan -> plano pago, o fallback, a confirmação do
provider/modelo efetivo e a limpeza da sessão é o `zcode-worker` (o mesmo wrapper que
atende o orquestrador). A ponte só valida a entrada, resolve modo/cwd/effort e repassa
o prompt (pelo STDIN) ao worker:

  GET  /v1/models            -> catalogo fixo do plano: glm-5.3-flash e glm-5.3
  GET  /healthz              -> capacidade/defaults (sem dados de chamadas anteriores)
  POST /v1/chat/completions  -> formato OpenAI; executa
                                `zcode-worker --cwd <cwd> --mode <modo> --model <alias>
                                --effort <low|high|max> --chat --json
                                --queue-timeout 30 --timeout <TIMEOUT>` com o prompt
                                no stdin; o worker responde JSON
                                {"status":"ok","sessionId","response","usage"}
  POST /v1/messages          -> formato Anthropic (Messages): a resposta final em
                                bloco "text"

Por chamada:
  model                 -> "glm-5.3-flash" | "glm-5.3" (default glm-5.3-flash);
                           presente e nao-string = 400, string desconhecida = 400.
                           O alias publico vai direto no --model do worker; nada de
                           copiar provider_config, escolher providerId ou conferir
                           o banco.
  effort                -> reasoning_effort (Chat) ou output_config.effort /
                           thinking.budget_tokens (Messages), mapeado para --effort:
                           low/minimal/medium->low, high->high, xhigh/max->max;
                           ausente->high; invalido = 400.
  modos                 -> somente "plan" e "edit" (build/yolo/auto removidos; valor
                           fora da lista = 400). O modo do chat vem do ULTIMO match de
                           "<collaboration_mode>" seguido da linha "# Collaboration
                           Mode: <palavra>" em mensagens system/developer: "plan"
                           ativa plan e VENCE o header X-ZCode-Mode; outra palavra,
                           nenhum match ou bloco sem cabecalho deixa valer o header,
                           senao ASK_BRIDGE_MODE (default "plan"). Prose que apenas
                           cita "<collaboration_mode>...</collaboration_mode>" nao
                           casa (nao ha cabecalho na sequencia).
  cwd                   -> header X-Ask-Cwd (diretorio absoluto existente; invalido =
                           400) ou o ULTIMO <cwd>...</cwd> dentro de blocos completos
                           <environment_context>...</environment_context> de uma
                           mensagem cujo environment_context abre ou fecha o content
                           (mensagens system/developer; a role user nunca entra no
                           texto de controle e so contribui com cwd quando o content,
                           apos remover TODOS os blocos top-level <tag>...</tag>,
                           deixa so whitespace — ex.: <recommended_plugins> e
                           <environment_context> juntos). Texto livre fora de blocos
                           = mensagem comum, ignorada; <cwd> fora do bloco e
                           ignorado; cwd do contexto invalido = 400, sem fallback
                           para HOME. Sem nenhum cwd: plan -> $HOME; edit -> 400.

O corpo aceita no maximo 4 MiB (Content-Length invalido/ausente/maior = 400 antes
da leitura); o prompt concatenado aceita no maximo MAX_PROMPT_BYTES (120_000) bytes
UTF-8 — vai pelo stdin do worker; excedido = 400.

A resposta Chat Completions devolve o model solicitado e os headers de diagnostico
X-ZCode-Model, X-ZCode-Mode e X-ZCode-Cwd (URL-encoded) antes do body. Erros: 400
entrada/modo/cwd/model/effort invalido, 504 timeout, 502 falha do zcode-worker —
mensagem curta, sem stack trace, sem credenciais e sem saida do worker (no journal
vai so o tamanho de stdout/stderr). Nao ha tool calls nativas. Com "stream": true a
ponte responde 200 + text/event-stream imediatamente, emite batimentos (ping SSE /
evento ping do Messages) enquanto o worker trabalha — SEM progresso ao vivo de
ferramentas — e, no fim, transmite o conteudo final inteiro como delta(s) normais
([DONE] no Chat; message_delta/message_stop no Messages). O bloco
⟪progresso⟫…⟪/progresso⟫ de historico antigo continua removido nos turnos seguintes
(nunca volta ao modelo). Cada chamada usa subprocesso proprio (prompt pelo stdin); o
contador diario de consumo continua protegido por lock.

Servico: systemd user `ask-glm-bridge.service`, porta 10110 (só loopback).
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

# modulo comum ao lado deste arquivo: formato/erros Anthropic e SSE Messages
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ask_bridge_anthropic as anth  # noqa: E402

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


PORT = int(os.environ.get("ASK_BRIDGE_PORT", "10110"))
ZCODE_WORKER = os.environ.get("ASK_GLM_ZCODE_WORKER",
                              os.path.expanduser("~/.local/bin/zcode-worker"))
TIMEOUT = int(os.environ.get("ASK_BRIDGE_TIMEOUT", "1800"))

# Catalogo publico = somente os modelos confirmados do plano (nada de regex no bundle).
CATALOGO = {
    "glm-5.3-flash": "GLM-5.3-Flash",
    "glm-5.3": "GLM-5.3",
}
MODELO_DEFAULT = "glm-5.3-flash"
# sentinel: corpo SEM a chave "model" (distinto de "model": null, que da 400)
_SEM_MODEL = object()

# effort pedido -> --effort do zcode-worker (nao existe "medium" no worker).
RACIOCINIO_FLASH = {"minimal": "low", "low": "low", "medium": "low",
                    "high": "high", "xhigh": "max", "max": "max"}
MODOS = ("plan", "edit")
DEFAULT_MODE = os.environ.get("ASK_BRIDGE_MODE", "plan")
MAX_BODY = 4 * 1024 * 1024
# O prompt vai pelo stdin do zcode-worker; o teto de entrada continua protegido.
MAX_PROMPT_BYTES = 120_000

RX_CWD_TAG = re.compile(r"<cwd>([\s\S]*?)</cwd>")
RX_ENV_CTX = re.compile(r"<environment_context>([\s\S]*?)</environment_context>", re.S)
# Formato do app ZCode (provider openai-chat): linha no prompt de sistema.
RX_ZCODE_CWD = re.compile(r"^- Primary working directory: (.+?)\s*$", re.M)
# App ZCode em modo Plan: lembrete injetado na mensagem user do turno atual.
RX_ZCODE_PLAN = re.compile(r"<system-reminder>\s*(?:# Plan Mode - System Reminder\s*)?Plan mode is active\.", re.I)


def _plan_zcode_ativo(messages) -> bool:
    """Plan do app ZCode: lembrete em mensagem user DEPOIS da ultima resposta do assistant.

    So restringe (plan nunca libera edicao), entao aceitar a role user e fail-closed. O
    recorte no turno atual evita ficar preso em Plan por lembretes de turnos antigos.
    """
    ultimas = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            ultimas = []
        elif m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(t for t in (_texto_do_item(x) for x in c) if t)
            if isinstance(c, str):
                ultimas.append(c)
    return any(RX_ZCODE_PLAN.search(c) for c in ultimas)
RX_COLAB_HEADER = re.compile(r"<collaboration_mode>\s*#\s*Collaboration Mode:\s*(\w+)", re.I)

# Progresso ao vivo no stream SSE vai como reasoning_content (sem marcas no stream);
# RX_PROGRESSO mantem a limpeza das marcas ⟪progresso⟫…⟪/progresso⟫ de historico
# antigo antes de voltar ao modelo.
MARCA_INI = "⟪progresso⟫"
MARCA_FIM = "⟪/progresso⟫"
RX_PROGRESSO = re.compile(re.escape(MARCA_INI) + r"[\s\S]*?" + re.escape(MARCA_FIM) + r"\s*")
HEARTBEAT_S = 1


class ErroEntrada(Exception):
    """Requisicao invalida -> HTTP 400."""


class ErroZcode(Exception):
    """Falha do CLI do ZCode ou divergencia de modelo -> HTTP 502."""


class ClienteDesconectou(Exception):
    """Cliente fechou a conexao durante o stream SSE -> so loga, sem resposta."""


id_seq = 0


def novo_id() -> str:
    global id_seq
    id_seq += 1
    return f"ask-bridge-{int(time.time())}-{id_seq}"


def _texto_do_item(item) -> str:
    """Texto de um item de content array (text / input_text / output_text)."""
    if not isinstance(item, dict):
        return ""
    if str(item.get("type", "")).lower().startswith(("tool", "function")):
        return ""  # schemas/resultado de ferramenta nao entram no prompt
    for chave in ("text", "input_text", "output_text"):
        v = item.get(chave)
        if isinstance(v, str) and v:
            return v
    return ""


def prompt_das_messages(messages) -> str:
    """Concatena a conversa em texto para o -p do zcode-cli.

    Preserva content string e arrays com itens text/input_text/output_text. Tool
    schemas (corpo.tools e itens tool_*/function nos arrays) nunca entram no prompt.
    O bloco de progresso (⟪progresso⟫…⟪/progresso⟫) de respostas assistant
    anteriores e removido antes de montar o texto.
    """
    partes = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        papel = m.get("role", "user")
        c = m.get("content")
        if isinstance(c, list):  # conteudo multimodal: pega so os trechos de texto
            c = " ".join(t for t in (_texto_do_item(p) for p in c) if t)
        if c is None:
            c = ""
        c = str(c)
        if papel in ("system", "developer"):
            partes.append(f"[instrucoes] {c}")
        elif papel == "assistant":
            partes.append(f"[resposta anterior] {RX_PROGRESSO.sub('', c)}")
        else:
            partes.append(c)
    return "\n\n".join(partes).strip()


def extrair_controle(messages) -> tuple[str, list[str]]:
    """Texto de controle (so system/developer) e cwds na ordem original das mensagens.

    texto_controle reune SOMENTE mensagens system/developer (content string ou array
    de itens text/input_text); a role user NUNCA entra — logo nao influencia
    resolver_modo. A role user so contribui para os cwds quando o bloco
    <environment_context>...</environment_context> ABRE ou FECHA o content (strip),
    que e como o OpenCodex entrega o contexto injetado pelo Codex (varios itens
    concatenados, environment_context por ultimo); usa-se o ultimo bloco dessa
    mensagem. Bloco no meio de texto comum e ignorado. assistant, tool,
    function e demais roles sao ignoradas. cwds sao as tags <cwd>...</cwd> dentro
    desses blocos, montadas na ordem original (user e system/developer
    intercalados); <cwd> fora deles e ignorado.
    """
    partes = []
    cwds = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        papel = m.get("role")
        c = m.get("content")
        if isinstance(c, list):  # conteudo multimodal: pega so os trechos de texto
            c = " ".join(t for t in (_texto_do_item(p) for p in c) if t)
        if papel in ("system", "developer"):
            if isinstance(c, str) and c:
                partes.append(c)
                for bloco in RX_ENV_CTX.findall(c):
                    cwds.extend(RX_CWD_TAG.findall(bloco))
                # app ZCode: "- Primary working directory: <dir>" no prompt de sistema
                cwds.extend(RX_ZCODE_CWD.findall(c))
                for bloco in re.findall(r"<env>([\s\S]*?)</env>", c):
                    cwds.extend(re.findall(r"^  Working directory: (.+?)\s*$", bloco, re.M))
        elif papel == "user":
            # Contexto injetado pelo Codex: o OpenCodex concatena varios itens
            # (plugins, AGENTS.md, environment_context) numa unica mensagem user,
            # com texto livre entre eles. Vale o bloco environment_context que
            # ABRE ou FECHA a mensagem; bloco no meio de texto comum e ignorado.
            if isinstance(c, str):
                t = c.strip()
                if t.startswith("<environment_context>") or t.endswith("</environment_context>"):
                    blocos = RX_ENV_CTX.findall(t)
                    if blocos:
                        cwds.extend(RX_CWD_TAG.findall(blocos[-1]))
    return "\n".join(partes), cwds


def resolver_modo(headers, messages) -> str:
    """Modo do chat: o ULTIMO cabecalho "# Collaboration Mode:" vence o header HTTP.

    RX_COLAB_HEADER casa "<collaboration_mode>" seguido da linha "# Collaboration
    Mode: <palavra>"; o ULTIMO match em texto_controle define o modo ("plan",
    case-insensitive -> plan; outro match ou nenhum nao e plan). Prose que apenas
    cita "<collaboration_mode>...</collaboration_mode>" nao casa (falta o cabecalho
    na sequencia, e nao depende mais de fechamento de bloco). Sem Plan ativo:
    header X-ZCode-Mode (plan|edit), senao DEFAULT_MODE.
    """
    modo_header = None
    bruto = headers.get("X-ZCode-Mode")
    if bruto is not None:
        modo_header = bruto.strip().lower()
        if modo_header not in MODOS:
            raise ErroEntrada(f"modo invalido; permitidos: {', '.join(MODOS)}")
    texto, _ = extrair_controle(messages)
    palavras = RX_COLAB_HEADER.findall(texto)
    if palavras and palavras[-1].lower() == "plan":
        return "plan"
    if _plan_zcode_ativo(messages):
        return "plan"  # Plan do app ZCode tambem vence o header  # Plan do chat nunca e superado pelo header
    if modo_header is not None:
        return modo_header
    if DEFAULT_MODE not in MODOS:
        raise ErroEntrada(f"ASK_BRIDGE_MODE invalido; permitidos: {', '.join(MODOS)}")
    return DEFAULT_MODE


def _diretorio_valido(caminho: str):
    """Caminho absoluto, expandido e resolvido de um diretorio existente; senao None."""
    p = os.path.expanduser((caminho or "").strip())
    if not p or not os.path.isabs(p):
        return None
    p = os.path.realpath(p)
    return p if os.path.isdir(p) else None


def resolver_cwd(headers, messages, modo: str) -> str:
    """Header X-Ask-Cwd > ultimo <cwd> em environment_context do controle do chat.

    Header presente e invalido = 400. cwd do contexto presente e invalido = 400 (sem
    fallback silencioso para HOME). Sem nenhum cwd: modo plan -> $HOME; modo edit ->
    400 (edit nunca roda sem cwd explicito).
    """
    if "X-Ask-Cwd" in headers:
        cwd = _diretorio_valido(headers.get("X-Ask-Cwd"))
        if cwd is None:
            raise ErroEntrada("X-Ask-Cwd invalido (diretorio absoluto existente esperado)")
        return cwd
    _, cwds = extrair_controle(messages)
    if cwds:
        cwd = _diretorio_valido(cwds[-1])
        if cwd is None:
            raise ErroEntrada("cwd do environment_context invalido")
        return cwd
    if modo == "plan":
        return os.path.expanduser("~")
    raise ErroEntrada("modo edit exige cwd explicito (X-Ask-Cwd ou environment_context)")


class Progresso:
    """Linhas de progresso emitidas via callback, opcionalmente entre marcas.

    Com marcas=True (padrao), evento() abre o bloco com MARCA_INI na primeira
    linha e fechar() encerra com MARCA_FIM. Com marcas=False (progresso via
    canal de raciocinio no stream) saem so as linhas, sem delimitadores.
    texto() emite trecho bruto (sem quebra automatica de linha), abrindo a
    marca se preciso. heartbeat() mantem o canal vivo em chamadas longas sem
    novidade: com batimento (callback externo, ex. ping SSE) dispara-o sempre
    que o intervalo vence, mesmo sem nada aberto; sem, emite "…" se houver
    bloco aberto. Nunca entra aqui a saida (output) das ferramentas nem o
    conteudo do prompt.
    """

    def __init__(self, emitir, marcas=True, batimento=None):
        self.emitir = emitir
        self.marcas = marcas
        self.batimento = batimento
        self.aberto = False
        self.ultimo = time.monotonic()

    def evento(self, linha: str):
        if not self.aberto:
            if self.marcas:
                self.emitir(MARCA_INI + "\n")
            self.aberto = True
        self.emitir(linha + "\n")
        self.ultimo = time.monotonic()

    def texto(self, t: str):
        if not t:
            return
        if not self.aberto:
            if self.marcas:
                self.emitir(MARCA_INI + "\n")
            self.aberto = True
        self.emitir(t)
        self.ultimo = time.monotonic()

    def heartbeat(self):
        if time.monotonic() - self.ultimo < HEARTBEAT_S:
            return
        if self.batimento is not None:
            self.batimento()
        elif self.aberto:
            self.emitir("…\n")
        else:
            return
        self.ultimo = time.monotonic()

    def fechar(self):
        if self.aberto:
            if self.marcas:
                self.emitir(MARCA_FIM + "\n\n")
            self.aberto = False


def rodar_zcode(prompt: str, cwd: str, modo: str, modelo_publico: str,
                effort=None, progresso=None):
    """Executa o zcode-worker com o prompt pelo stdin; retorna (json, sessionId).

    Princípio do dono: o worker decide provider/modelo efetivo (rotação Start
    Plan -> plano pago), faz fallback e confirma o que usou — a ponte só passa
    cwd/modo/modelo-publico/effort. Effort e mapeado para low/high/max
    (RACIOCINIO_FLASH); ausente = high. Com progresso (Progresso), roda via
    Popen e chama heartbeat periodicamente enquanto espera — SEM progresso de
    ferramentas. Timeout do subprocesso: TIMEOUT + 60 (folga sobre o --timeout
    do proprio worker).
    """
    if effort is not None and (not isinstance(effort, str) or effort.lower() not in RACIOCINIO_FLASH):
        raise ErroEntrada("effort inválido")
    nivel = RACIOCINIO_FLASH[effort.lower()] if effort else "high"
    cmd = [ZCODE_WORKER, "--cwd", cwd, "--mode", modo, "--model", modelo_publico,
           "--effort", nivel, "--json", "--queue-timeout", "30", "--timeout", str(TIMEOUT)]
    cmd += ["--chat"] if modo == "plan" else []  # edit: preâmbulo de worker (regras de segurança)
    env = dict(os.environ)
    env["ASK_PRESENTATION_ACTIVE"] = "1"
    entrada = prompt.encode("utf-8")
    if progresso is None:
        p = subprocess.run(cmd, input=entrada, capture_output=True, env=env,
                           timeout=TIMEOUT + 60)  # run mata o filho no timeout
    else:
        p = _rodar_com_batimento(cmd, entrada, env, progresso)

    saida = p.stdout.decode("utf-8", "replace").strip()
    ini = saida.find("{")
    d = None
    if ini >= 0:
        try:
            bruto = json.loads(saida[ini:])
            if isinstance(bruto, dict):
                d = bruto
        except ValueError:
            d = None
    status = str(d.get("status") or "") if isinstance(d, dict) else ""
    erro = d.get("error") if isinstance(d, dict) else None
    codigo = ""
    if isinstance(erro, dict):
        codigo = str(erro.get("code") or "")
    elif isinstance(erro, str):
        codigo = erro
    if p.returncode == 124 or "timeout" in status.lower() or "timeout" in codigo.lower():
        raise subprocess.TimeoutExpired(cmd, TIMEOUT + 60)
    if (p.returncode == 0 and isinstance(d, dict) and status == "ok"
            and isinstance(d.get("sessionId"), str) and d["sessionId"]
            and isinstance(d.get("response"), str)):
        return d, d["sessionId"]
    # log so com tamanhos: saida do worker pode conter conteudo do usuario
    print(f"[bridge] zcode-worker rc={p.returncode} stdout={len(p.stdout)}B "
          f"stderr={len(p.stderr)}B", flush=True)
    raise ErroZcode(f"zcode falhou (rc={p.returncode})")


def _rodar_com_batimento(cmd, entrada: bytes, env, progresso):
    """Worker via Popen com heartbeat do stream enquanto ele nao responde.

    O prompt vai pelo communicate(input=...), que escreve stdin e le stdout/stderr
    sem deadlock de pipe. A cada HEARTBEAT_S sem resposta, progresso.heartbeat() —
    que mantem o stream vivo (ping SSE / "…"). No prazo (TIMEOUT + 60) ou se o
    heartbeat desistir (cliente sumiu), mata o worker antes de propagar:
    nenhum zcode-worker fica orfao atras do stream.
    """
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env)

    prazo = time.monotonic() + TIMEOUT + 60
    try:
        while True:
            restante = prazo - time.monotonic()
            if restante <= 0:
                raise subprocess.TimeoutExpired(cmd, TIMEOUT + 60)
            try:
                # input so e escrito na 1a chamada; nas retomadas apos timeout o subprocess ignora
                saida, erro = p.communicate(input=entrada, timeout=min(HEARTBEAT_S, restante))
                return subprocess.CompletedProcess(cmd, p.returncode, saida, erro)
            except subprocess.TimeoutExpired:
                progresso.heartbeat()
    finally:
        if p.poll() is None:
            p.terminate()  # SIGTERM: o zcode-worker encerra o zcode-cli filho
        try:
            p.wait(timeout=10)
        except Exception:  # noqa: BLE001 — worker teimoso: SIGKILL e segue
            p.kill()


def resposta_chat(conteudo: str, modelo: str, usage: dict | None = None) -> dict:
    u = usage or {}
    return {
        "id": novo_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": modelo,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": conteudo}}],
        "usage": {
            "prompt_tokens": u.get("inputTokens", 0),
            "completion_tokens": u.get("outputTokens", 0),
            "total_tokens": u.get("inputTokens", 0) + u.get("outputTokens", 0),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def _headers_diag(self, modelo, modo, cwd):
        """Headers de diagnóstico seguros (URL-encoded, sem caracteres de controle)."""
        extras = []
        if modelo:
            extras.append(("X-ZCode-Model", quote(str(modelo), safe="")))
        if modo:
            extras.append(("X-ZCode-Mode", quote(str(modo), safe="")))
        if cwd is not None:
            extras.append(("X-ZCode-Cwd", quote(str(cwd), safe="")))
        return extras

    def _json(self, code: int, obj: dict, extras=()):
        body = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in extras:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            print("[bridge] cliente desconectou antes da resposta", flush=True)

    def _erro(self, code: int, msg: str, modelo=None, modo=None, cwd=None):
        self._json(code, {"error": {"message": msg}},
                   self._headers_diag(modelo, modo, cwd))

    def do_GET(self):
        if self.path.split("?", 1)[0].rstrip("/") == "/v1/models":
            self._json(200, anth.modelos_anthropic(sorted(CATALOGO)))
        elif self.path.split("?", 1)[0].rstrip("/") == "/healthz":
            # Somente defaults/capacidades — nunca estado (cwd/modelo) de chamadas antigas.
            self._json(200, {
                "ok": True,
                "zcode_worker": ZCODE_WORKER,
                "plan_owner": "zcode-worker",
                "timeout_s": TIMEOUT,
                "max_prompt_bytes": MAX_PROMPT_BYTES,
                "modes": list(MODOS),
                "default_mode": DEFAULT_MODE,
                "models": sorted(CATALOGO),
                "model_selection": dict(CATALOGO),
                "anthropic_messages": True,
                "capabilities": {
                    "native_tool_calls": False,
                    "live_streaming": False,
                    "modes": ["plan", "edit"],
                },
            })
        else:
            self._json(404, {"error": {"message": "rota desconhecida"}})

    def _ler_corpo(self) -> dict:
        """Corpo JSON da requisicao: Content-Length valido e objeto; senao ErroEntrada."""
        bruto_len = self.headers.get("Content-Length")
        try:
            tamanho = int(bruto_len) if bruto_len is not None else 0
        except ValueError:
            raise ErroEntrada("Content-Length invalido") from None
        if tamanho <= 0 or tamanho > MAX_BODY:
            raise ErroEntrada(f"Content-Length deve ser entre 1 e {MAX_BODY} bytes")
        try:
            corpo = json.loads(self.rfile.read(tamanho) or b"{}")
        except ValueError:
            raise ErroEntrada("corpo JSON invalido") from None
        if not isinstance(corpo, dict):
            raise ErroEntrada("corpo deve ser um objeto JSON")
        if "stream" in corpo and type(corpo["stream"]) is not bool:
            raise ErroEntrada("stream deve ser booleano")
        return corpo

    def _preparar(self, corpo_model, messages) -> tuple:
        """Validacao comum as duas rotas: model, messages, prompt, modo e cwd.

        corpo_model = _SEM_MODEL quando a chave "model" esta ausente (usa o default);
        qualquer valor presente nao-string da 400, como hoje. Sem executar nada.
        Retorna (modelo, texto, modo, cwd).
        """
        if corpo_model is _SEM_MODEL:
            alias = MODELO_DEFAULT
        elif not isinstance(corpo_model, str):
            raise ErroEntrada("model deve ser string")
        else:
            alias = corpo_model
        if alias not in CATALOGO:
            raise ErroEntrada(f"model desconhecido; use um de {sorted(CATALOGO)}")
        modelo = alias

        if not isinstance(messages, list) or not all(
                isinstance(m, dict) for m in messages):
            raise ErroEntrada("messages deve ser lista de objetos")

        texto = prompt_das_messages(messages)
        if not texto:
            raise ErroEntrada("sem prompt")
        try:
            tamanho_texto = len(texto.encode("utf-8", "strict"))
        except UnicodeError:
            raise ErroEntrada("prompt UTF-8 inválido") from None
        if "\0" in texto:
            raise ErroEntrada("prompt contém NUL")
        if tamanho_texto > MAX_PROMPT_BYTES:
            raise ErroEntrada(
                f"prompt excede {MAX_PROMPT_BYTES} bytes (limite de argumento do zcode-cli)")

        modo = resolver_modo(self.headers, messages)
        cwd = resolver_cwd(self.headers, messages, modo)
        return modelo, texto, modo, cwd

    def do_POST(self):
        rota = self.path.rstrip("/")
        if rota == "/v1/messages":
            self._do_messages()
            return
        if rota != "/v1/chat/completions":
            self._json(404, {"error": {"message": "rota desconhecida"}})
            return
        modelo = modo = None
        cwd = None
        stream_iniciado = False
        try:
            corpo = self._ler_corpo()
            modelo, texto, modo, cwd = self._preparar(
                corpo.get("model", _SEM_MODEL), corpo.get("messages"))
            # effort pedido (Chat Completions): reasoning_effort (str) ou reasoning.effort
            e = corpo.get("reasoning_effort")
            if not isinstance(e, str) or not e:
                r = corpo.get("reasoning")
                if isinstance(r, dict) and isinstance(r.get("effort"), str):
                    e = r.get("effort")
            effort = e or None

            streaming = bool(corpo.get("stream"))
            if streaming:
                # 200 + headers SSE saem ANTES do worker; daqui em diante nenhum erro
                # vira 502/504 — vira delta "[erro da ponte] ..." no proprio stream
                self._iniciar_stream(modelo, modo, cwd)
                stream_iniciado = True
                prog = Progresso(lambda t: self._delta(modelo, t, raciocinio=True),
                                 marcas=False, batimento=self._ping_chat)
                try:
                    d, _ = rodar_zcode(texto, cwd, modo, modelo,
                                       progresso=prog, effort=effort)
                finally:
                    prog.fechar()
            else:
                d, _ = rodar_zcode(texto, cwd, modo, modelo, effort=effort)

            u = d.get("usage") or {}
            conteudo = d.get("response", "")
            acumular("glm", u.get("inputTokens", 0), u.get("outputTokens", 0))
            if streaming:
                self._delta(modelo, conteudo)
                self._fim_stream(modelo)
            else:
                self._responder_chat(modelo, modo, cwd, conteudo, u)
        except ClienteDesconectou as e:
            print(f"[bridge] {e}", flush=True)
        except ErroEntrada as e:
            self._erro_ou_delta(stream_iniciado, modelo, modo, cwd, 400, str(e))
        except subprocess.TimeoutExpired:
            self._erro_ou_delta(stream_iniciado, modelo, modo, cwd, 504,
                                f"zcode excedeu {TIMEOUT}s")
        except ErroZcode as e:
            self._erro_ou_delta(stream_iniciado, modelo, modo, cwd, 502, str(e))
        except (BrokenPipeError, ConnectionResetError):
            print("[bridge] cliente desconectou", flush=True)
        except Exception as e:  # noqa: BLE001 — log curto no journal, segredo nunca
            print(f"[bridge] erro interno: {type(e).__name__}", flush=True)
            self._erro_ou_delta(stream_iniciado, modelo, modo, cwd, 502,
                                "falha interna na ponte")

    def _do_messages(self):
        """POST /v1/messages: formato Anthropic sobre o zcode-worker.

        Resposta final em bloco text; batimentos (evento ping) enquanto o worker
        trabalha — sem progresso ao vivo de ferramentas. O "effort" e mapeado
        para --effort do worker (low/minimal/medium->low, high->high,
        xhigh/max->max). Erros antes do stream = HTTP anth.erro_anthropic;
        depois = sse.erro.
        """
        modelo = modo = None
        cwd = None
        stream_iniciado = False
        self._sse = None
        try:
            corpo = self._ler_corpo()
            messages = anth.anthropic_para_chat(corpo)
            effort = anth.effort_de_corpo(corpo)
            modelo, texto, modo, cwd = self._preparar(
                corpo.get("model", _SEM_MODEL), messages)
            streaming = bool(corpo.get("stream"))
            if streaming:
                self._iniciar_stream(modelo, modo, cwd)
                stream_iniciado = True
                sse = anth.SSEAnthropic(self._escrever_sse, modelo)
                self._sse = sse
                sse.iniciar()
                prog = Progresso(sse.pensamento, marcas=False, batimento=sse.ping)
                try:
                    d, _ = rodar_zcode(texto, cwd, modo, modelo,
                                       progresso=prog, effort=effort)
                finally:
                    prog.fechar()
            else:
                d, _ = rodar_zcode(texto, cwd, modo, modelo, effort=effort)

            u = d.get("usage") or {}
            conteudo = d.get("response", "")
            acumular("glm", u.get("inputTokens", 0), u.get("outputTokens", 0))
            if streaming:
                sse.texto(conteudo)
                sse.fim(u.get("outputTokens", 0), tokens_in=u.get("inputTokens", 0))
            else:
                self._json(200, anth.mensagem_anthropic(
                    modelo, conteudo, u.get("inputTokens", 0),
                    u.get("outputTokens", 0)), self._headers_diag(modelo, modo, cwd))
        except anth.ErroFormato as e:
            self._erro_anth(400, "invalid_request_error", str(e), modelo, modo, cwd)
        except ErroEntrada as e:
            self._erro_ou_sse(stream_iniciado, modelo, modo, cwd, 400, "invalid_request_error", str(e))
        except subprocess.TimeoutExpired:
            self._erro_ou_sse(stream_iniciado, modelo, modo, cwd, 504, "timeout_error",
                              f"zcode excedeu {TIMEOUT}s")
        except ErroZcode as e:
            self._erro_ou_sse(stream_iniciado, modelo, modo, cwd, 502, "api_error",
                              str(e))
        except ClienteDesconectou as e:
            print(f"[bridge] {e}", flush=True)
        except (BrokenPipeError, ConnectionResetError):
            print("[bridge] cliente desconectou", flush=True)
        except Exception as e:  # noqa: BLE001 — log curto no journal, segredo nunca
            print(f"[bridge] erro interno: {type(e).__name__}", flush=True)
            self._erro_ou_sse(stream_iniciado, modelo, modo, cwd, 502, "api_error",
                              "falha interna na ponte")

    def _erro_anth(self, code: int, tipo: str, msg: str, modelo=None, modo=None,
                   cwd=None):
        """Erro no formato Anthropic (anth.erro_anthropic) + headers de diagnostico."""
        self._json(code, anth.erro_anthropic(tipo, msg),
                   self._headers_diag(modelo, modo, cwd))

    def _escrever_sse(self, dados: bytes):
        """Callback escrever(bytes) do SSEAnthropic; queda de conexao vira
        ClienteDesconectou (o stream nunca derruba a thread com erro bruto)."""
        try:
            self.wfile.write(dados)
            self.wfile.flush()
        except OSError:  # BrokenPipe, ConnectionReset/Aborted, timeout de socket
            raise ClienteDesconectou("cliente desconectou durante o stream") from None

    def _erro_ou_sse(self, stream_iniciado, modelo, modo, cwd, code, tipo, msg):
        """Erro antes do stream = HTTP (formato Anthropic); depois = sse.erro + log."""
        if not stream_iniciado:
            self._erro_anth(code, tipo, msg, modelo, modo, cwd)
            return
        print(f"[bridge] stream com erro: {msg}", flush=True)
        sse = getattr(self, "_sse", None)
        if sse is None:
            return
        try:
            sse.erro(msg)
        except ClienteDesconectou:
            print("[bridge] cliente desconectou durante o stream de erro", flush=True)

    def _responder_chat(self, modelo, modo, cwd, conteudo, usage=None):
        self._json(200, resposta_chat(conteudo, modelo, usage),
                   self._headers_diag(modelo, modo, cwd))

    def _iniciar_stream(self, modelo, modo, cwd):
        """Abre o SSE: 200 + text/event-stream + no-cache + headers de diagnostico."""
        self._stream_id = novo_id()
        self._primeiro_delta = True
        # escrita SSE nunca pode travar a thread alem de 30s (cliente preso)
        self.connection.settimeout(5)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            for k, v in self._headers_diag(modelo, modo, cwd):
                self.send_header(k, v)
            self.end_headers()
        except OSError:  # BrokenPipe, ConnectionReset/Aborted, timeout de socket
            raise ClienteDesconectou("cliente desconectou ao iniciar o stream") from None

    def _delta(self, modelo, texto, raciocinio=False):
        """Chunk SSE; com raciocinio=True vai {"reasoning_content": texto} (progresso
        ao vivo, exibido pelo Codex em "Pensando"); sem, {"content": texto}, com
        "role": "assistant" no primeiro delta de conteudo.
        """
        if raciocinio:
            delta = {"reasoning_content": texto}
        else:
            delta = {"content": texto}
            if getattr(self, "_primeiro_delta", True):
                delta["role"] = "assistant"
                self._primeiro_delta = False
        chunk = {"id": getattr(self, "_stream_id", None) or novo_id(),
                 "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": modelo,
                 "choices": [{"index": 0, "finish_reason": None, "delta": delta}]}
        try:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        except OSError:  # BrokenPipe, ConnectionReset/Aborted, timeout de socket
            raise ClienteDesconectou("cliente desconectou durante o stream") from None

    def _fim_stream(self, modelo):
        """Chunk final finish_reason stop + data: [DONE]."""
        fim = {"id": getattr(self, "_stream_id", None) or novo_id(),
               "object": "chat.completion.chunk", "created": int(time.time()),
               "model": modelo,
               "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]}
        try:
            self.wfile.write(f"data: {json.dumps(fim)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:  # BrokenPipe, ConnectionReset/Aborted, timeout de socket
            raise ClienteDesconectou("cliente desconectou no fim do stream") from None

    def _erro_ou_delta(self, stream_iniciado, modelo, modo, cwd, code, msg):
        """Erro antes do stream = HTTP; depois do stream = delta curto + fim."""
        if not stream_iniciado:
            self._erro(code, msg, modelo, modo, cwd)
            return
        print(f"[bridge] stream com erro: {msg}", flush=True)
        try:
            self._escrever_sse(("data: " + json.dumps({"error": {"type": "api_error", "message": msg}})
                                + "\n\ndata: [DONE]\n\n").encode("utf-8"))
        except ClienteDesconectou:
            print("[bridge] cliente desconectou durante o stream de erro", flush=True)

    def _ping_chat(self):
        self._escrever_sse(b": ping\n\n")

    def log_message(self, fmt, *args):  # log enxuto no journal
        print(f"[bridge] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    if not os.path.exists(ZCODE_WORKER):
        raise SystemExit(f"zcode-worker nao encontrado: {ZCODE_WORKER}")
    print(f"[bridge] ouvindo 127.0.0.1:{PORT} mode_default={DEFAULT_MODE} "
          f"modelos={sorted(CATALOGO)} worker={ZCODE_WORKER}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
