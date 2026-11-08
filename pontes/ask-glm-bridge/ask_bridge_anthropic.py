"""Módulo para falar o formato Anthropic Messages (``POST /v1/messages``),
consumido pelo adaptador ``anthropic`` do OpenCodex.

Fonte de verdade: esta cópia em `pontes/ask-glm-bridge/`. A cópia da
ask-claude-bridge (fora deste repo) é derivada e deve ser atualizada a
partir daqui.

Oferece: extração do "effort" do corpo, conversão Anthropic -> mensagens
estilo Chat Completions, montagem de Message/erro/lista de modelos e o
escritor SSE do stream. Somente stdlib; Python 3.10+.
"""

from __future__ import annotations

import json
import uuid

EFFORTS_ANTHROPIC = ("low", "medium", "high", "xhigh", "max")


class ErroFormato(ValueError):
    """Corpo /v1/messages invalido (a ponte responde 400)."""


def _texto_de_blocos(blocos) -> str:
    """Junta com "\\n\\n" o campo text dos blocos dict com type == "text"; ignora os demais."""
    partes = []
    for b in blocos:
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
            partes.append(b["text"])
    return "\n\n".join(partes)


def effort_de_corpo(corpo: dict) -> str | None:
    """Extrai o effort (output_config.effort ou thinking.budget_tokens); nunca levanta."""
    if not isinstance(corpo, dict):
        return None
    cfg = corpo.get("output_config")
    if isinstance(cfg, dict):
        e = cfg.get("effort")
        if isinstance(e, str):
            e = e.strip().lower()
            if e in EFFORTS_ANTHROPIC:
                return e
    thinking = corpo.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        b = thinking.get("budget_tokens")
        if isinstance(b, int) and not isinstance(b, bool):
            if b >= 32000:
                return "max"
            if b >= 24576:
                return "xhigh"
            if b >= 16384:
                return "high"
            if b >= 8192:
                return "medium"
            return "low"
    return None


def anthropic_para_chat(corpo: dict) -> list[dict]:
    """Converte o corpo /v1/messages em mensagens estilo Chat Completions ([{"role", "content": str}])."""
    if not isinstance(corpo, dict):
        raise ErroFormato("corpo deve ser um objeto JSON")
    saida: list[dict] = []
    system = corpo.get("system")
    if isinstance(system, str):
        if system:
            saida.append({"role": "system", "content": system})
    elif isinstance(system, list):
        texto = _texto_de_blocos(system)
        if texto:
            saida.append({"role": "system", "content": texto})
    elif system is not None:
        raise ErroFormato("system invalido")
    mensagens = corpo.get("messages")
    if not isinstance(mensagens, list):
        raise ErroFormato("messages deve ser lista de objetos")
    for m in mensagens:
        if not isinstance(m, dict):
            raise ErroFormato("messages deve ser lista de objetos")
        role = m.get("role")
        if role not in ("user", "assistant"):
            raise ErroFormato("role invalido")
        content = m.get("content")
        if isinstance(content, str):
            texto = content
        elif isinstance(content, list):
            texto = _texto_de_blocos(content)
        else:
            raise ErroFormato("content invalido")
        if not texto.strip():
            continue
        saida.append({"role": role, "content": texto})
    return saida


def mensagem_anthropic(modelo: str, texto: str, tokens_in: int = 0, tokens_out: int = 0) -> dict:
    """Monta o objeto Message completo (resposta não-stream) do formato Anthropic."""
    return {
        "id": "msg_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": modelo,
        "content": [{"type": "text", "text": texto}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(tokens_in or 0),
            "output_tokens": int(tokens_out or 0),
        },
    }


def erro_anthropic(tipo: str, mensagem: str) -> dict:
    """Monta o corpo de erro do formato Anthropic ({"type": "error", "error": {...}})."""
    return {"type": "error", "error": {"type": tipo, "message": mensagem}}


def modelos_anthropic(ids) -> dict:
    """Monta a lista de modelos compatível com OpenAI e Anthropic ao mesmo tempo."""
    lista = list(ids)
    return {
        "object": "list",
        "data": [
            {
                "id": i,
                "object": "model",
                "type": "model",
                "display_name": i,
                "created_at": "2026-01-01T00:00:00Z",
                "owned_by": "local-bridge",
            }
            for i in lista
        ],
        "has_more": False,
        "first_id": (lista[0] or None) if lista else None,
        "last_id": (lista[-1] or None) if lista else None,
    }


class SSEAnthropic:
    """Escreve o stream SSE no formato Anthropic Messages, um evento por chamada, com flush."""

    def __init__(self, escrever, modelo: str):
        """escrever: callable(bytes) que já faz write+flush; exceções dele PROPAGAM (a ponte trata)."""
        self._escrever = escrever
        self._modelo = modelo
        self.indice = -1
        self.bloco: str | None = None  # "thinking" | "text" | None
        self.iniciado = False
        self.fechado = False

    def _evento(self, tipo: str, dados: dict):
        """Escreve um evento SSE completo (event + data), codificado em UTF-8."""
        linha = f"event: {tipo}\ndata: {json.dumps(dados, ensure_ascii=False)}\n\n"
        self._escrever(linha.encode("utf-8"))

    def iniciar(self):
        """Emite message_start uma única vez."""
        if self.iniciado or self.fechado:
            return
        self.iniciado = True
        self._evento("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg_" + uuid.uuid4().hex,
                "type": "message",
                "role": "assistant",
                "model": self._modelo,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

    def _abrir(self, tipo: str):
        """Garante o bloco corrente do tipo dado (fecha o anterior se for outro tipo)."""
        if self.bloco == tipo:
            return
        if self.bloco is not None:
            self._evento("content_block_stop", {"type": "content_block_stop", "index": self.indice})
        self.indice += 1
        novo = {"type": "thinking", "thinking": ""} if tipo == "thinking" else {"type": "text", "text": ""}
        self._evento("content_block_start", {
            "type": "content_block_start",
            "index": self.indice,
            "content_block": novo,
        })
        self.bloco = tipo

    def pensamento(self, texto: str):
        """Emite um trecho de raciocínio (delta thinking_delta)."""
        if not texto or self.fechado:
            return
        self.iniciar()
        self._abrir("thinking")
        self._evento("content_block_delta", {
            "type": "content_block_delta",
            "index": self.indice,
            "delta": {"type": "thinking_delta", "thinking": texto},
        })

    def texto(self, texto: str):
        """Emite um trecho de resposta (delta text_delta)."""
        if not texto or self.fechado:
            return
        self.iniciar()
        self._abrir("text")
        self._evento("content_block_delta", {
            "type": "content_block_delta",
            "index": self.indice,
            "delta": {"type": "text_delta", "text": texto},
        })

    def ping(self):
        """Emite um evento ping sem abrir bloco (mantém a conexão viva)."""
        if self.fechado:
            return
        self.iniciar()
        self._evento("ping", {"type": "ping"})

    def _fechar_bloco(self):
        """Fecha o bloco de conteúdo aberto, se houver."""
        if self.bloco is not None:
            self._evento("content_block_stop", {"type": "content_block_stop", "index": self.indice})
            self.bloco = None

    def erro(self, mensagem: str, tipo: str = "api_error"):
        """Fecha o stream com um evento error (idempotente)."""
        if self.fechado:
            return
        self._fechar_bloco()
        self._evento("error", erro_anthropic(tipo, mensagem))
        self.fechado = True

    def fim(self, tokens_out: int = 0, stop_reason: str = "end_turn", tokens_in=None):
        """Fecha o stream com message_delta + message_stop (idempotente)."""
        if self.fechado:
            return
        self.iniciar()
        self._fechar_bloco()
        usage = {"output_tokens": int(tokens_out or 0)}
        if tokens_in is not None:
            usage["input_tokens"] = int(tokens_in)
        self._evento("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": usage,
        })
        self._evento("message_stop", {"type": "message_stop"})
        self.fechado = True
