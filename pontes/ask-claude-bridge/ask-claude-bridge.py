#!/usr/bin/env python3
"""ask-claude-bridge — ponte HTTP local (OpenAI-compatible) -> CLI do Claude Code oficial.

Faz com que o OpenCodex (ou qualquer cliente OpenAI-compatible) use o Claude atraves do
Claude Code instalado (assinatura oficial), e nao por chave de API. Cada chamada roda
`claude -p --permission-mode <modo> --output-format json --model <modelo>`
(prompt entra por stdin).

  GET  /v1/models            -> catalogo fixo (env ASK_CLAUDE_MODELS; default
                                claude-fable-5-1, claude-opus-5-5, claude-sonnet-5-5,
                                claude-haiku-5-5, claude-opus-5, claude-sonnet-5, claude-haiku-4-5,
                                claude-haiku-4-5-20251001, claude-fable-5)
  GET  /healthz              -> capacidade/defaults (sem dados de chamadas anteriores)
  POST /v1/chat/completions  -> roda o claude e devolve resposta Chat Completions
  POST /v1/messages          -> idem no formato Anthropic Messages, com ou sem stream:
                                o progresso vai em bloco thinking — que o OpenCodex
                                converte em resumo de raciocinio visivel ao vivo no app
                                do Codex — e a resposta final em bloco text

Por chamada:
  model      -> id do catalogo (default claude-fable-5-1); presente e nao-string = 400;
                desconhecido = 400, sem fallback silencioso.
  reasoning_effort -> (low|medium|high|xhigh|max) vira --effort do CLI; valor fora da lista = 400.
  modos      -> somente "plan" e "edit" (valor fora da lista = 400); viram
                --permission-mode plan/acceptEdits. O modo do chat vem do ULTIMO
                cabecalho "# Collaboration Mode: <palavra>" dentro de um bloco
                <collaboration_mode> em mensagens system/developer (casado sem
                exigir o fechamento do bloco): "Plan" ativa plan e VENCE o header
                X-Claude-Mode; "Default"/outra palavra/sem match deixa valer o
                header X-Claude-Mode (plan|edit), senao ASK_CLAUDE_BRIDGE_MODE
                (default plan). A palavra "plan" solta no corpo do bloco nao conta.
  cwd        -> header X-Ask-Cwd (diretorio absoluto existente; invalido = 400) ou o
                ULTIMO <cwd>...</cwd> dentro de blocos completos
                <environment_context>...</environment_context> do texto de controle
                (mensagens system/developer; role user entra como mensagem de
                controle: so blocos top-level <tag>...</tag>, sem texto livre).
                cwd do contexto invalido = 400, sem fallback. Sem nenhum
                cwd: plan -> $HOME; edit -> 400.

O corpo aceita no maximo 4 MiB (Content-Length invalido/ausente/maior = 400 antes da
leitura). Pos-execucao, o modelo realmente usado e confirmado pelas chaves de
modelUsage do JSON de saida: divergencia ou modelUsage ausente = 502.

A resposta devolve o model solicitado e os headers de diagnostico X-Claude-Model,
X-Claude-Mode e X-Claude-Cwd (URL-encoded) antes do body. Erros: 400
entrada/modo/cwd/model invalido, 504 timeout, 502 falha do claude ou divergencia de
modelo — mensagem curta, sem stack trace, sem credenciais e sem saida do CLI (no
journal vai so o tamanho de stdout/stderr). Nao ha tool calls nativas. Com "stream":
true o SSE e ao vivo: enquanto o claude roda, a ponte transmite o progresso textual
das ferramentas (▶ inicio, ✔/✖ fim) como reasoning_content no Chat Completions —
o app do Codex exibe ao vivo em "Pensando" —, seguido do chunk de conteudo, chunk
final e [DONE]; na conversa seguinte qualquer bloco antigo entre ⟪progresso⟫ e
⟪/progresso⟫ e removido do historico (prompt_das_messages), nunca voltando ao modelo.
Em /v1/messages o mesmo motor responde no formato Anthropic Messages: o progresso
(titulo, eventos ▶/✔/✖ das ferramentas e o pensamento parcial do CLI) vai como bloco
thinking do SSE — que o OpenCodex converte em resumo de raciocinio visivel ao vivo no
app do Codex — e a resposta final como bloco text.
O subprocesso roda SEM ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN (caminho
nativo da assinatura, nunca proxy), com o prompt entrando por stdin; o contador
diario de consumo continua protegido por lock.

Servico: systemd user `ask-claude-bridge.service`, porta 10111 (so loopback).
"""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ask_bridge_anthropic as anth
from ask_bridge_stream import ReconciliadorTexto, FalhaStreaming, executar_limitado

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


PORT = int(os.environ.get("ASK_CLAUDE_BRIDGE_PORT", "10111"))
CLAUDE_BIN = os.environ.get("ASK_CLAUDE_BIN", "claude")
TIMEOUT = int(os.environ.get("ASK_CLAUDE_BRIDGE_TIMEOUT", "1200"))

MODOS = ("plan", "edit")
MODO_CLI = {"plan": "plan", "edit": "acceptEdits"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODE = os.environ.get("ASK_CLAUDE_BRIDGE_MODE", "plan")
MAX_BODY = 4 * 1024 * 1024

# Catalogo publico fixo (env ASK_CLAUDE_MODELS); nada de regex no binario do CLI.
CATALOGO = tuple(m.strip() for m in os.environ.get(
    "ASK_CLAUDE_MODELS",
    "claude-fable-5-1,claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5,claude-opus-5,"
    "claude-sonnet-5,claude-haiku-4-5,claude-haiku-4-5-20251001,claude-fable-5",
).split(",") if m.strip())
MODELO_DEFAULT = CATALOGO[0]

RX_CWD_TAG = re.compile(r"<cwd>([\s\S]*?)</cwd>")
RX_ENV_CTX = re.compile(r"<environment_context>([\s\S]*?)</environment_context>", re.S)
# Formato do app ZCode (provider openai-chat): linha no prompt de sistema.
RX_ZCODE_CWD = re.compile(r"^- Primary working directory: (.+?)\s*$", re.M)
# App ZCode em modo Plan: lembrete injetado na mensagem user do turno atual.
RX_ZCODE_PLAN = re.compile(r"<system-reminder>\s*Plan mode is active\.", re.I)


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
# Mensagem de controle (role user): apos remover TODOS os blocos top-level
# <tag>...</tag>, so sobra whitespace — aceita varios blocos colados numa
# mensagem so (ex.: <recommended_plugins> + <environment_context> do OpenCodex).
# Cabecalho de modo dentro do bloco collaboration_mode, casado sem depender do
# fechamento do bloco (a prosa pode conter "</collaboration_mode>" literal).
RX_COLAB_HEADER = re.compile(r"<collaboration_mode>\s*#\s*Collaboration Mode:\s*(\w+)", re.I)

# Marcas do bloco de progresso: o stream atual emite o progresso como
# reasoning_content, sem marcas; RX_PROGRESSO existe para limpar do historico
# blocos antigos que ainda as carregam (nunca volta ao modelo).
MARCA_INI = "⟪progresso⟫"
MARCA_FIM = "⟪/progresso⟫"
# Titulo do bloco thinking no stream /v1/messages.
TITULO_PROGRESSO = "**Claude Code trabalhando**"
RX_PROGRESSO = re.compile(re.escape(MARCA_INI) + r"[\s\S]*?" + re.escape(MARCA_FIM) + r"\s*")
HEARTBEAT_S = 1


class ErroEntrada(Exception):
    """Requisicao invalida -> HTTP 400."""


class ErroClaude(Exception):
    """Falha do CLI do Claude Code ou divergencia de modelo -> HTTP 502."""


class ClienteDesconectou(Exception):
    """Cliente fechou a conexao durante o stream SSE -> apenas loga."""


id_seq = 0


def novo_id() -> str:
    global id_seq
    id_seq += 1
    return f"ask-claude-bridge-{int(time.time())}-{id_seq}"


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
    """Concatena a conversa em texto para o -p do claude.

    Preserva content string e arrays com itens text/input_text/output_text. Tool
    schemas (corpo.tools e itens tool_*/function nos arrays) nunca entram no prompt.
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
            # bloco de progresso de turnos anteriores nunca volta ao modelo
            partes.append(f"[resposta anterior] {RX_PROGRESSO.sub('', c)}")
        else:
            partes.append(c)
    return "\n\n".join(partes).strip()


def extrair_controle(messages) -> tuple[str, list[str]]:
    """Texto de controle (SOMENTE system/developer) e cwds do chat.

    No texto_controle entram apenas mensagens system/developer (content string ou
    array de itens text/input_text); a role user NUNCA entra — logo nunca
    influencia resolver_modo. assistant, tool, function e demais roles sao
    ignoradas. Os cwds vem de tags <cwd>...</cwd> DENTRO de blocos
    environment_context completos, coletados na ordem original das mensagens
    (system/developer e user intercalados): (a) blocos do texto_controle
    (system/developer); (b) role user cujo content (strip) ABRE ou FECHA com um
    bloco <environment_context>...</environment_context> — e como o OpenCodex
    entrega o contexto injetado pelo Codex (varios itens concatenados, com
    environment_context por ultimo); usa-se o ultimo bloco dessa mensagem.
    Bloco no meio de texto comum e ignorado. <cwd> fora desses blocos e ignorado.
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
    """Modo do chat: Plan do ULTIMO cabecalho <collaboration_mode> vence o header.

    O cabecalho "# Collaboration Mode: <palavra>" e procurado dentro de blocos
    <collaboration_mode> do texto de controle (RX_COLAB_HEADER, sem exigir o
    fechamento do bloco — a prosa pode conter "</collaboration_mode>" literal) e
    vale o ULTIMO match: "plan" -> plan; outra palavra/sem match nao e plan. A
    palavra "plan" solta no corpo nao conta. Sem Plan ativo: header X-Claude-Mode
    (plan|edit), senao DEFAULT_MODE.
    """
    modo_header = None
    bruto = headers.get("X-Claude-Mode")
    if bruto is not None:
        modo_header = bruto.strip().lower()
        if modo_header not in MODOS:
            raise ErroEntrada(f"modo invalido; permitidos: {', '.join(MODOS)}")
    texto, _ = extrair_controle(messages)
    cabecalhos = RX_COLAB_HEADER.findall(texto)
    if cabecalhos and cabecalhos[-1].strip().lower() == "plan":
        return "plan"  # Plan do chat nunca e superado pelo header
    if _plan_zcode_ativo(messages):
        return "plan"  # Plan do app ZCode tambem vence o header
    if modo_header is not None:
        return modo_header
    if DEFAULT_MODE not in MODOS:
        raise ErroEntrada(f"ASK_CLAUDE_BRIDGE_MODE invalido; permitidos: {', '.join(MODOS)}")
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


CHAVES_RESUMO = ("command", "cmd", "file_path", "path", "filePath",
                 "pattern", "query", "url", "description")


def _uma_linha_corta(texto, limite: int = 100) -> str:
    """Texto numa linha so (quebras viram espaco), cortado em `limite` chars com …."""
    texto = " ".join(str(texto).split())
    if len(texto) > limite:
        texto = texto[:limite] + "…"
    return texto


def _resumo_input(tool: str, entrada) -> str:
    """Resumo do input de uma ferramenta para a linha de progresso (1 linha).

    Entrada pode ser dict ou string JSON (json.loads se str; falha -> usa a str).
    Prefere command/cmd/file_path/path/filePath/pattern/query/url/description;
    senao o proprio JSON (ou str). Nunca inclui saida de ferramenta.
    """
    if isinstance(entrada, str):
        try:
            entrada = json.loads(entrada)
        except ValueError:
            pass
    valor = None
    if isinstance(entrada, dict):
        for chave in CHAVES_RESUMO:
            v = entrada.get(chave)
            if isinstance(v, str) and v:
                valor = v
                break
    if valor is None:
        try:
            valor = json.dumps(entrada, ensure_ascii=False)
        except (TypeError, ValueError):
            valor = str(entrada)
    return _uma_linha_corta(valor)


def _texto_tool_result(bloco: dict) -> str:
    """Texto de um tool_result (para o ✖ curto; a saida completa nunca e emitida)."""
    c = bloco.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(t for t in (
            i.get("text") for i in c
            if isinstance(i, dict) and i.get("type") == "text"
            and isinstance(i.get("text"), str)
        ) if t)
    return ""


class Progresso:
    """Eventos de ferramentas emitidos ao vivo entre as marcas de progresso.

    emitir(texto) recebe cada trecho pronto (no stream: um chunk SSE). A saida
    (output) das ferramentas e o conteudo do prompt NUNCA passam por aqui.
    marcas=False omite os delimitadores ⟪progresso⟫/⟪/progresso⟫ (o progresso
    vai para canal proprio, ex.: delta.reasoning_content ou bloco thinking).
    batimento=None: callable de ping; presente, o heartbeat chama-o a cada
    HEARTBEAT_S de silencio (mesmo sem bloco aberto) em vez de emitir '…'.
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
        """Trecho bruto de fala/pensamento, sem quebra de linha acrescentada."""
        if not t:
            return
        if not self.aberto and self.marcas:
            self.emitir(MARCA_INI + "\n")
        self.aberto = True
        self.emitir(t)
        self.ultimo = time.monotonic()

    def heartbeat(self):
        """Com batimento, pinga a cada HEARTBEAT_S de silencio; sem, emite '…'
        se o bloco estiver aberto."""
        if self.batimento is not None:
            if time.monotonic() - self.ultimo >= HEARTBEAT_S:
                self.batimento()
                self.ultimo = time.monotonic()
        elif self.aberto and time.monotonic() - self.ultimo >= HEARTBEAT_S:
            self.emitir("…\n")
            self.ultimo = time.monotonic()

    def fechar(self):
        if self.aberto:
            if self.marcas:
                self.emitir(MARCA_FIM + "\n\n")
            self.aberto = False


LIMITES_CACHE = os.path.expanduser("~/.local/state/ask-bridges/claude-limits.json")


def gravar_limites(info) -> None:
    """Grava uso 5h/semanal do rate_limit_event no formato do statusline.

    Fonte do ask-limites-zcode quando nao ha sessao do Claude Code no terminal.
    `utilization` vem como fracao 0..1; o statusline usa porcentagem 0..100.
    So grava com as duas janelas presentes. Falha nunca afeta a conversa.
    """
    try:
        janelas = (info or {}).get("unifiedWindows") or {}
        rl = {}
        for nome in ("five_hour", "seven_day"):
            j = janelas.get(nome)
            if not isinstance(j, dict) or not isinstance(j.get("utilization"), (int, float)):
                return
            rl[nome] = {"used_percentage": round(j["utilization"] * 100, 1),
                        "resets_at": j.get("resetsAt")}
        os.makedirs(os.path.dirname(LIMITES_CACHE), exist_ok=True)
        # temporario exclusivo por chamada: threads concorrentes nao se atropelam
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(LIMITES_CACHE), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"at": int(time.time() * 1000), "rate_limits": rl,
                           "fonte": "ask-claude-bridge"}, f)
            os.replace(tmp, LIMITES_CACHE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, TypeError, ValueError, AttributeError):
        pass


def rodar_claude(prompt: str, cwd: str, modo: str, modelo: str, progresso=None,
                 effort=None, texto_vivo=None) -> dict:
    """Executa o Claude Code oficial (-p) e devolve o objeto de saida completo.

    Sem progresso: --output-format json e subprocess.run com input=prompt (caminho
    atual). Com progresso: --output-format stream-json --verbose via Popen; eventos
    tool_use/tool_result viram linhas do bloco de progresso (▶ inicio, ✔/✖ fim);
    saida de ferramenta e conteudo do prompt nunca sao emitidos.
    """
    # caminho nativo: sem overrides de proxy no ambiente, como no claude-ask
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")}
    if progresso is None:
        cmd = [CLAUDE_BIN, "-p", "--permission-mode", MODO_CLI[modo],
               "--output-format", "json", "--model", modelo]
        if effort is not None:
            cmd += ["--effort", effort]
        # argv em lista com shell=False: prompt/cwd nunca passam por interpretador;
        # prompt vai por stdin (input=), sem esperar entrada herdada do servico
        p = executar_limitado(cmd, cwd=cwd, env=env, timeout=TIMEOUT, entrada=prompt)
        try:
            d = json.loads(p.stdout.strip())
        except ValueError:
            d = {}
        if p.returncode != 0 or d.get("is_error") \
                or d.get("subtype") not in (None, "success") or "result" not in d:
            # log so com tamanhos: stdout/stderr/result podem conter conteudo do usuario
            print(f"[claude-bridge] claude rc={p.returncode} subtype={d.get('subtype')!r} "
                  f"stdout={len(p.stdout)}B stderr={len(p.stderr)}B", flush=True)
            raise ErroClaude(f"claude falhou (rc={p.returncode}, subtype={d.get('subtype')})")
        return d
    return _rodar_claude_stream(prompt, cwd, modo, modelo, env, progresso,
                               effort=effort, texto_vivo=texto_vivo)


def _matar_grupo(p: subprocess.Popen) -> None:
    """SIGKILL no grupo do CLI e descendentes (Popen com sessao propria) + wait 5s."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _rodar_claude_stream(prompt: str, cwd: str, modo: str, modelo: str, env: dict,
                         progresso: "Progresso", effort=None, texto_vivo=None) -> dict:
    """Consume only main-assistant native deltas; verify before emitting text."""
    cmd = [CLAUDE_BIN, "-p", "--permission-mode", MODO_CLI[modo],
           "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--model", modelo]
    if effort is not None:
        cmd += ["--effort", effort]
    resultado = None
    pendentes = {}
    buffer_texto = []
    verified = False
    message_id = None
    message_complete = True
    local_message = 0

    def confirmar(visto):
        nonlocal verified
        if visto is None:
            return
        if not isinstance(visto, str) or not (visto == modelo or re.fullmatch(re.escape(modelo) + r"-\d{8}", visto)):
            raise ErroClaude("modelo nativo diverge do solicitado")
        verified = True
        if texto_vivo is not None:
            texto_vivo.liberar()

    def fala_intermediaria():
        if texto_vivo is None and buffer_texto:
            progresso.texto("\n" + "".join(buffer_texto) + "\n")
            buffer_texto.clear()

    def linha(raw):
        nonlocal resultado, message_id, message_complete, local_message
        try:
            ev = json.loads(raw)
        except ValueError:
            return
        if not isinstance(ev, dict) or ev.get("parent_tool_use_id") is not None:
            return
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            confirmar(ev.get("model"))
        elif kind == "rate_limit_event":
            gravar_limites(ev.get("rate_limit_info"))
        elif kind == "stream_event":
            event = ev.get("event")
            if not isinstance(event, dict):
                return
            event_type = event.get("type")
            if event_type == "message_start":
                if not message_complete and texto_vivo is not None and texto_vivo.ultimo_texto:
                    raise ErroClaude("mensagem nativa reiniciada após texto parcial")
                message = event.get("message") or {}
                confirmar(message.get("model"))
                message_id = message.get("id")
                if message_id is None:
                    local_message += 1
                    message_id = f"local-{local_message}"
                message_complete = False
                buffer_texto.clear()
                if texto_vivo is not None and verified:
                    texto_vivo.abrir_segmento(message_id)
            elif event_type == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if texto_vivo is not None:
                        if verified and message_id is not None:
                            texto_vivo.delta(text)
                    else:
                        buffer_texto.append(text)
                elif delta.get("type") == "thinking_delta" and (verified or texto_vivo is None):
                    progresso.texto(delta.get("thinking") or "")
            elif event_type == "content_block_start":
                if (event.get("content_block") or {}).get("type") == "tool_use":
                    fala_intermediaria()
            elif event_type == "message_delta":
                if (event.get("delta") or {}).get("stop_reason") == "tool_use":
                    fala_intermediaria()
            elif event_type == "message_stop":
                message_complete = True
            elif event_type == "error":
                raise ErroClaude("erro no stream nativo")
        elif kind in ("assistant", "user"):
            if texto_vivo is not None and not verified:
                return
            for block in (ev.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    identity = block.get("id")
                    if not isinstance(identity, str) or len(pendentes) >= 256:
                        raise ErroClaude("limite ou identidade de ferramenta inválidos")
                    name = str(block.get("name") or "ferramenta").replace("\n", " ")[:80]
                    pendentes[identity] = name
                    progresso.evento("▶ " + name)
                elif block.get("type") == "tool_result":
                    name = pendentes.pop(block.get("tool_use_id"), None)
                    if name is not None:
                        progresso.evento(("✖ " if block.get("is_error") else "✔ ") + name)
        elif kind == "result":
            if resultado is not None:
                raise ErroClaude("resultado nativo duplicado")
            resultado = ev

    p = executar_limitado(cmd, cwd=cwd, env=env, timeout=TIMEOUT, entrada=prompt,
                          por_linha=linha, pulso=progresso.heartbeat)
    if p.returncode != 0 or resultado is None or resultado.get("is_error") or \
            resultado.get("subtype") != "success" or not isinstance(resultado.get("result"), str):
        print(f"[claude-bridge] native rc={p.returncode} stdout={len(p.stdout)} stderr={len(p.stderr)}", flush=True)
        raise ErroClaude("Claude Code não concluiu com sucesso")
    status, _ = verificar_modelo(resultado, modelo)
    if status != "ok":
        raise ErroClaude("modelo final não confirmado")
    if not verified and any(key != modelo and not re.fullmatch(re.escape(modelo) + r"-\d{8}", key)
                            for key in (resultado.get("modelUsage") or {})):
        raise ErroClaude("modelo principal não confirmado")
    return resultado


def verificar_modelo(d: dict, modelo: str) -> tuple[str, str]:
    """Confere o modelo realmente usado pelas chaves de modelUsage da saida JSON.

    Retorna ("ok", chave_vista) | ("diverge", chaves_vistas) | ("indefinido", motivo).
    So conta como ok a chave IGUAL ao modelo pedido ou a variante snapshot datada
    (fullmatch de <modelo>-AAAAMMDD). Prefixo generico NAO vale: pedido
    "claude-fable-5" com uso "claude-fable-5-1" = ("diverge", ...).
    """
    usados = list((d.get("modelUsage") or {}).keys())
    if not usados:
        return ("indefinido", "modelUsage ausente")
    for chave in usados:
        if chave == modelo or re.fullmatch(re.escape(modelo) + r"-\d{8}", chave):
            return ("ok", chave)
    return ("diverge", ", ".join(usados))


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
            "prompt_tokens": u.get("input_tokens", 0),
            "completion_tokens": u.get("output_tokens", 0),
            "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def _headers_diag(self, modelo, modo, cwd):
        """Headers de diagnóstico seguros (URL-encoded, sem caracteres de controle)."""
        extras = []
        if modelo:
            extras.append(("X-Claude-Model", quote(str(modelo), safe="")))
        if modo:
            extras.append(("X-Claude-Mode", quote(str(modo), safe="")))
        if cwd is not None:
            extras.append(("X-Claude-Cwd", quote(str(cwd), safe="")))
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
            print("[claude-bridge] cliente desconectou antes da resposta", flush=True)

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
                "bin": CLAUDE_BIN,
                "timeout_s": TIMEOUT,
                "modes": list(MODOS),
                "efforts": list(EFFORTS),
                "anthropic_messages": True,
                "mode_cli": MODO_CLI,
                "default_mode": DEFAULT_MODE,
                "models": list(CATALOGO),
                "capabilities": {
                    "native_tool_calls": False,
                    "live_streaming": True,
                    "modes": ["plan", "edit"],
                    "progress_marker": MARCA_INI,
                    "progress_channel": "reasoning_content",
                    "anthropic_messages": True,
                },
            })
        else:
            self._json(404, {"error": {"message": "rota desconhecida"}})

    def _ler_corpo(self) -> dict:
        """Le e valida o corpo JSON (dict); Content-Length/JSON invalido = 400."""
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
        """Validacao comum as duas rotas POST: (modelo, texto, modo, cwd)."""
        if corpo_model is not None and not isinstance(corpo_model, str):
            raise ErroEntrada("model deve ser string")
        modelo = corpo_model or MODELO_DEFAULT
        if modelo not in CATALOGO:
            raise ErroEntrada(f"model desconhecido; use um de {list(CATALOGO)}")
        if not isinstance(messages, list) or not all(
                isinstance(m, dict) for m in messages):
            raise ErroEntrada("messages deve ser lista de objetos")
        texto = prompt_das_messages(messages)
        if not texto:
            raise ErroEntrada("sem prompt")
        try:
            tamanho = len(texto.encode("utf-8", "strict"))
        except UnicodeError:
            raise ErroEntrada("prompt UTF-8 inválido") from None
        if "\0" in texto or tamanho > MAX_BODY:
            raise ErroEntrada("prompt inválido ou acima do limite")
        modo = resolver_modo(self.headers, messages)
        cwd = resolver_cwd(self.headers, messages, modo)
        return (modelo, texto, modo, cwd)

    def do_POST(self):
        if self.path.rstrip("/") == "/v1/messages":
            self._do_messages()
            return
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._json(404, {"error": {"message": "rota desconhecida"}})
            return
        modelo = modo = cwd = None
        stream_ativo = False
        try:
            corpo = self._ler_corpo()

            effort = corpo.get("reasoning_effort")
            if effort is not None:
                if not isinstance(effort, str) or effort.strip().lower() not in EFFORTS:
                    raise ErroEntrada(
                        f"reasoning_effort invalido; permitidos: {', '.join(EFFORTS)}")
                effort = effort.strip().lower()

            messages = corpo.get("messages")
            modelo, texto, modo, cwd = self._preparar(corpo.get("model"), messages)

            if corpo.get("stream"):
                stream_ativo = True
                self._iniciar_stream(modelo, modo, cwd)
                prog = Progresso(lambda t: self._delta(modelo, t, raciocinio=True),
                                 marcas=False, batimento=self._ping_chat)
                vivo = ReconciliadorTexto(lambda t: self._delta(modelo, t))
                try:
                    d = rodar_claude(texto, cwd, modo, modelo, progresso=prog,
                                     effort=effort, texto_vivo=vivo)
                finally:
                    prog.fechar()
                status, info = verificar_modelo(d, modelo)
                if status == "ok":
                    u = d.get("usage") or {}
                    acumular("claude", u.get("input_tokens", 0), u.get("output_tokens", 0),
                             float(d.get("total_cost_usd") or 0.0))
                    vivo.liberar()
                    vivo.finalizar(d.get("result", ""))
                    self._fim_stream(modelo)
                else:
                    msg = (f"modelo usado ({info}) difere do solicitado ({modelo})"
                           if status == "diverge"
                           else f"confirmacao de modelo falhou: {info}")
                    self._erro_no_stream(modelo, msg)
            else:
                d = rodar_claude(texto, cwd, modo, modelo, effort=effort)

                status, info = verificar_modelo(d, modelo)
                if status == "diverge":
                    raise ErroClaude(
                        f"modelo usado ({info}) difere do solicitado ({modelo})")
                if status == "indefinido":
                    raise ErroClaude(f"confirmacao de modelo falhou: {info}")

                u = d.get("usage") or {}
                acumular("claude", u.get("input_tokens", 0), u.get("output_tokens", 0),
                         float(d.get("total_cost_usd") or 0.0))
                self._responder_chat(corpo, modelo, modo, cwd, d.get("result", ""), u)
        except ErroEntrada as e:
            self._erro(400, str(e), modelo, modo, cwd)
        except subprocess.TimeoutExpired:
            if stream_ativo:
                self._erro_no_stream(modelo, f"claude excedeu {TIMEOUT}s")
            else:
                self._erro(504, f"claude excedeu {TIMEOUT}s", modelo, modo, cwd)
        except (ErroClaude, FalhaStreaming) as e:
            if stream_ativo:
                self._erro_no_stream(modelo, str(e))
            else:
                self._erro(502, str(e), modelo, modo, cwd)
        except ClienteDesconectou:
            print("[claude-bridge] cliente desconectou durante o stream", flush=True)
        except (BrokenPipeError, ConnectionResetError):
            print("[claude-bridge] cliente desconectou", flush=True)
        except Exception as e:  # noqa: BLE001 — log curto no journal, segredo nunca
            print(f"[claude-bridge] erro interno: {type(e).__name__}", flush=True)
            if stream_ativo:
                self._erro_no_stream(modelo, "falha interna na ponte")
            else:
                self._erro(502, "falha interna na ponte", modelo, modo, cwd)

    def _escrever_sse(self, dados: bytes):
        """Escrita SSE bruta; desconexao/timeout de escrita vira ClienteDesconectou."""
        try:
            self.wfile.write(dados)
            self.wfile.flush()
        except OSError as e:  # BrokenPipe, Reset/Aborted, timeout de socket
            raise ClienteDesconectou from e

    def _erro_anthropic(self, code: int, tipo: str, msg: str,
                        modelo=None, modo=None, cwd=None):
        """Erro no formato Anthropic (antes do stream), com headers de diagnostico."""
        self._json(code, anth.erro_anthropic(tipo, msg),
                   self._headers_diag(modelo, modo, cwd))

    def _sse_erro_log(self, sse, msg: str):
        """Loga e manda o erro pelo stream Anthropic; cliente sumido nao re-loga."""
        print(f"[claude-bridge] {msg}", flush=True)
        try:
            sse.erro(msg)
        except (ClienteDesconectou, BrokenPipeError, ConnectionResetError):
            pass

    def _do_messages(self):
        """POST /v1/messages: mesmo motor, formato Anthropic Messages.

        Erros antes do stream vao como JSON anth.erro_anthropic (400
        invalid_request_error, 502 api_error, 504 timeout_error). Com stream:
        titulo + progresso + pensamento parcial do CLI em bloco thinking e resposta
        final em bloco text; sem stream, resposta unica em JSON.
        """
        modelo = modo = cwd = None
        try:
            corpo = self._ler_corpo()
            messages = anth.anthropic_para_chat(corpo)
            effort = anth.effort_de_corpo(corpo)
            if effort is not None and effort not in EFFORTS:
                effort = None
            modelo, texto, modo, cwd = self._preparar(corpo.get("model"), messages)

            if corpo.get("stream"):
                self._iniciar_stream(modelo, modo, cwd)
                sse = anth.SSEAnthropic(self._escrever_sse, modelo)
                try:
                    sse.iniciar()
                    sse.pensamento(TITULO_PROGRESSO + "\n\n")
                    prog = Progresso(sse.pensamento, marcas=False, batimento=sse.ping)
                    vivo = ReconciliadorTexto(sse.texto)
                    try:
                        d = rodar_claude(texto, cwd, modo, modelo, progresso=prog,
                                         effort=effort, texto_vivo=vivo)
                    finally:
                        prog.fechar()
                    status, info = verificar_modelo(d, modelo)
                    if status != "ok":
                        msg = (f"modelo usado ({info}) difere do solicitado ({modelo})"
                               if status == "diverge"
                               else f"confirmacao de modelo falhou: {info}")
                        raise ErroClaude(msg)
                    u = d.get("usage") or {}
                    acumular("claude", u.get("input_tokens", 0),
                             u.get("output_tokens", 0),
                             float(d.get("total_cost_usd") or 0.0))
                    vivo.liberar()
                    vivo.finalizar(d.get("result", ""))
                    sse.fim(u.get("output_tokens", 0), tokens_in=u.get("input_tokens", 0))
                except ClienteDesconectou:
                    print("[claude-bridge] cliente desconectou durante o stream",
                          flush=True)
                except subprocess.TimeoutExpired:
                    self._sse_erro_log(sse, f"claude excedeu {TIMEOUT}s")
                except (ErroClaude, FalhaStreaming) as e:
                    self._sse_erro_log(sse, str(e))
                except Exception as e:  # noqa: BLE001 — log curto no journal
                    print(f"[claude-bridge] erro interno: {type(e).__name__}",
                          flush=True)
                    self._sse_erro_log(sse, "falha interna na ponte")
                return

            d = rodar_claude(texto, cwd, modo, modelo, effort=effort)
            status, info = verificar_modelo(d, modelo)
            if status == "diverge":
                raise ErroClaude(
                    f"modelo usado ({info}) difere do solicitado ({modelo})")
            if status == "indefinido":
                raise ErroClaude(f"confirmacao de modelo falhou: {info}")
            u = d.get("usage") or {}
            acumular("claude", u.get("input_tokens", 0), u.get("output_tokens", 0),
                     float(d.get("total_cost_usd") or 0.0))
            self._json(200, anth.mensagem_anthropic(modelo, d.get("result", ""),
                                                    u.get("input_tokens", 0),
                                                    u.get("output_tokens", 0)),
                       self._headers_diag(modelo, modo, cwd))
        except anth.ErroFormato as e:
            self._erro_anthropic(400, "invalid_request_error", str(e), modelo, modo, cwd)
        except ErroEntrada as e:
            self._erro_anthropic(400, "invalid_request_error", str(e), modelo, modo, cwd)
        except subprocess.TimeoutExpired:
            self._erro_anthropic(504, "timeout_error", f"claude excedeu {TIMEOUT}s",
                                 modelo, modo, cwd)
        except (ErroClaude, FalhaStreaming) as e:
            self._erro_anthropic(502, "api_error", str(e), modelo, modo, cwd)
        except ClienteDesconectou:
            print("[claude-bridge] cliente desconectou durante o stream", flush=True)
        except (BrokenPipeError, ConnectionResetError):
            print("[claude-bridge] cliente desconectou", flush=True)
        except Exception as e:  # noqa: BLE001 — log curto no journal, segredo nunca
            print(f"[claude-bridge] erro interno: {type(e).__name__}", flush=True)
            self._erro_anthropic(502, "api_error", "falha interna na ponte",
                                 modelo, modo, cwd)

    def _iniciar_stream(self, modelo, modo, cwd):
        """Abre o SSE: status 200, headers de stream + diagnostico, end_headers."""
        self._stream_id = novo_id()
        self._delta_primeiro = True
        try:
            # teto de 30s na escrita SSE: cliente sumido nao trava a thread para sempre
            self.connection.settimeout(5)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            for k, v in self._headers_diag(modelo, modo, cwd):
                self.send_header(k, v)
            self.end_headers()
        except OSError as e:  # BrokenPipe, Reset/Aborted, timeout de socket
            raise ClienteDesconectou from e

    def _delta(self, modelo, texto: str, raciocinio: bool = False):
        """Um chunk SSE do stream: raciocinio=True emite reasoning_content
        (progresso ao vivo, sem content e sem tocar o 1o delta); False emite
        content, com role assistant no 1o delta de conteudo."""
        sid = getattr(self, "_stream_id", None) or novo_id()
        self._stream_id = sid
        if raciocinio:
            delta = {"reasoning_content": texto}
        else:
            primeiro = getattr(self, "_delta_primeiro", True)
            self._delta_primeiro = False
            delta = ({"role": "assistant", "content": texto}
                     if primeiro else {"content": texto})
        chunk = {"id": sid, "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": modelo,
                 "choices": [{"index": 0, "finish_reason": None, "delta": delta}]}
        try:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        except OSError as e:  # BrokenPipe, Reset/Aborted, timeout de socket
            raise ClienteDesconectou from e

    def _fim_stream(self, modelo):
        """Chunk final (finish_reason stop) + [DONE]."""
        sid = getattr(self, "_stream_id", None) or novo_id()
        fim = {"id": sid, "object": "chat.completion.chunk",
               "created": int(time.time()), "model": modelo,
               "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]}
        try:
            self.wfile.write(f"data: {json.dumps(fim)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError as e:  # BrokenPipe, Reset/Aborted, timeout de socket
            raise ClienteDesconectou from e

    def _erro_no_stream(self, modelo, msg: str):
        """Erro com headers ja enviados: vira delta [erro da ponte] + fim (com log)."""
        print(f"[claude-bridge] {msg}", flush=True)
        try:
            self._escrever_sse(("data: " + json.dumps({"error": {"type": "api_error", "message": msg}})
                                + "\n\ndata: [DONE]\n\n").encode("utf-8"))
        except (ClienteDesconectou, BrokenPipeError, ConnectionResetError):
            pass

    def _ping_chat(self):
        self._escrever_sse(b": ping\n\n")

    def _responder_chat(self, corpo, modelo, modo, cwd, conteudo, usage=None):
        extras = self._headers_diag(modelo, modo, cwd)
        if corpo.get("stream"):
            # SSE minimalista (formato atual): chunk de conteudo + chunk final + DONE.
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                for k, v in extras:
                    self.send_header(k, v)
                self.end_headers()
                chunk = {"id": novo_id(), "object": "chat.completion.chunk",
                         "created": int(time.time()), "model": modelo,
                         "choices": [{"index": 0, "finish_reason": None,
                                      "delta": {"role": "assistant", "content": conteudo}}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                fim = {"id": chunk["id"], "object": "chat.completion.chunk",
                       "created": int(time.time()), "model": modelo,
                       "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]}
                self.wfile.write(f"data: {json.dumps(fim)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                print("[claude-bridge] cliente desconectou durante o stream", flush=True)
        else:
            self._json(200, resposta_chat(conteudo, modelo, usage), extras)

    def log_message(self, fmt, *args):  # log enxuto no journal
        print(f"[claude-bridge] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    if shutil.which(CLAUDE_BIN) is None and not os.path.exists(CLAUDE_BIN):
        raise SystemExit("claude nao encontrado")
    print(f"[claude-bridge] ouvindo 127.0.0.1:{PORT} mode_default={DEFAULT_MODE} "
          f"modelos={list(CATALOGO)} bin={CLAUDE_BIN}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
