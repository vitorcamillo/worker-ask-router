"""Read one correlated native session through bounded, consistent SQLite snapshots."""
import hashlib
import json
import sqlite3
from urllib.parse import quote

from ask_bridge_stream import FalhaStreaming


def _rows_bounded(cursor):
    rows, size = [], 0
    for row in cursor:
        raw = row[-1]
        size += len(raw) if isinstance(raw, bytes) else len(str(raw).encode("utf-8"))
        if len(rows) >= 1024 or size > 16 * 1024 * 1024:
            raise FalhaStreaming("limite de snapshot excedido")
        rows.append(row)
    return rows


def _object(raw):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise FalhaStreaming("snapshot inválido") from None
    if not isinstance(value, dict):
        raise FalhaStreaming("snapshot inválido")
    return value


def poll_texto(dbpath, estado, cwd, t0_ms, progresso):
    conn = sqlite3.connect("file:" + quote(dbpath, safe="/") + "?mode=ro", uri=True, timeout=.1)
    try:
        conn.execute("BEGIN")
        if not estado.get("session_id"):
            rows = _rows_bounded(conn.execute(
                "WITH candidates AS (SELECT id FROM session WHERE directory=? AND time_created>=? "
                "ORDER BY time_created DESC LIMIT 17), first_user AS ("
                "SELECT m.session_id, min(m.sequence) seq FROM message m JOIN candidates c ON c.id=m.session_id "
                "WHERE json_extract(m.data,'$.role')='user' GROUP BY m.session_id) "
                "SELECT p.session_id,m.id,substr(CAST(p.data AS BLOB),1,16777217) FROM part p JOIN message m ON m.id=p.message_id "
                "JOIN first_user f ON f.session_id=m.session_id AND f.seq=m.sequence "
                "WHERE json_extract(p.data,'$.type')='text' ORDER BY m.sequence,p.sequence LIMIT 1025",
                (cwd, t0_ms)))
            prompts = {}
            for sid, mid, raw in rows:
                text = _object(raw).get("text")
                if not isinstance(text, str):
                    raise FalhaStreaming("texto de correlação inválido")
                prompts[(sid, mid)] = prompts.get((sid, mid), "") + text
            matches = {sid for (sid, _), text in prompts.items()
                       if hashlib.sha256(text.encode("utf-8")).hexdigest() == estado["prompt_sha256"]}
            if not matches:
                return
            if len(matches) != 1:
                raise FalhaStreaming("correlação de sessão ambígua")
            estado["session_id"] = matches.pop()
        sid = estado["session_id"]
        row = conn.execute("SELECT data FROM session_entry WHERE session_id=? AND type=? "
                           "ORDER BY time_created DESC LIMIT 1", (sid, "runtime/model_selection")).fetchone()
        if row is None:
            return
        selection = _object(row[0]).get("modelSelection")
        if not isinstance(selection, dict) or not isinstance(selection.get("options"), dict):
            raise FalhaStreaming("seleção nativa inválida")
        actual = (selection.get("providerId"), selection.get("modelId"), selection["options"].get("reasoningLevel"))
        if actual != estado["esperado"]:
            raise FalhaStreaming("provider/model/effort divergente")
        live = estado["texto_vivo"]
        if not live.liberado:
            live.liberar()
        rows = _rows_bounded(conn.execute(
            "SELECT m.id,p.id, CASE WHEN json_extract(p.data,'$.type')='text' THEN substr(CAST(p.data AS BLOB),1,16777217) "
            "WHEN json_extract(p.data,'$.type')='reasoning' THEN json_object('type','reasoning','text',"
            "substr(json_extract(p.data,'$.text'),1,1500)) ELSE json_object('type','tool','tool',"
            "json_extract(p.data,'$.tool'),'status',json_extract(p.data,'$.state.status')) END "
            "FROM part p JOIN message m ON m.id=p.message_id WHERE p.session_id=? "
            "AND json_extract(m.data,'$.role')='assistant' AND json_extract(p.data,'$.type') IN ('text','tool','reasoning') "
            "ORDER BY m.sequence,p.sequence LIMIT 1025", (sid,)))
        texts = {}
        seen = estado.setdefault("vistos", {})
        reasoning = estado.setdefault("raciocinio", {})
        for mid, _pid, raw in rows:
            part = _object(raw)
            if part.get("type") == "text":
                text = part.get("text")
                if not isinstance(text, str):
                    raise FalhaStreaming("snapshot de texto inválido")
                texts[mid] = texts.get(mid, "") + text
        emitted = set()
        for mid, pid, raw in rows:
            part = _object(raw)
            if part.get("type") == "text":
                if mid not in emitted:
                    live.snapshot(mid, texts[mid])
                    emitted.add(mid)
            elif part.get("type") == "tool":
                status = part.get("status")
                if pid not in seen or seen[pid] != status:
                    name = str(part.get("tool") or "ferramenta").replace("\n", " ")[:80]
                    progresso.evento(("✔ " if status == "completed" else "✖ " if status == "error" else "▶ ") + name)
                    seen[pid] = status
            else:
                text = part.get("text") or ""
                previous = reasoning.get(pid, "")
                if not isinstance(text, str) or not text.startswith(previous):
                    raise FalhaStreaming("snapshot de raciocínio reescrito")
                if text != previous:
                    progresso.texto(text[len(previous):])
                    reasoning[pid] = text
    finally:
        conn.close()


class EventosNativos:
    """Strict allowlist for the installed CLI's stream-json protocol.

    Root session + turn identify the principal assistant. Network status,
    aggregate messages, tool input/output and child sessions never escape.
    SQLite independently verifies the prompt and effective selection per start.
    """
    def __init__(self, texto, progresso, verificar):
        self.texto, self.progresso, self.verificar = texto, progresso, verificar
        self.session_id = self.turn_id = self.message_id = None
        self.completo = True
        self.turno_completo = True
        self.resultado = None
        self.seq = -1
        self.digest = None
        self.turnos = 0
        self.reasoning_chars = 0
        self.tools = {}

    @staticmethod
    def identidade(value):
        if not isinstance(value, str) or not value or len(value) > 256 or "\0" in value:
            raise FalhaStreaming("identidade nativa inválida")
        return value

    def linha(self, raw):
        event = _object(raw)
        tipo = event.get("type")
        if tipo == "result":
            if self.resultado is not None or not isinstance(event.get("response"), str):
                raise FalhaStreaming("resultado nativo inválido ou duplicado")
            sid = self.identidade(event.get("sessionId"))
            if self.session_id is not None and sid != self.session_id:
                raise FalhaStreaming("sessão final divergente")
            if not self.completo or not self.turno_completo:
                raise FalhaStreaming("resultado antes do fim nativo")
            self.verificar(sid)
            self.texto.liberar()
            self.resultado = {key: event[key] for key in ("response", "sessionId", "usage") if key in event}
            return
        if tipo not in ("turn.started", "turn.completed", "turn.failed", "model.streaming", "streamRecovery.updated", "tool.updated"):
            return  # No raw metadata, credentials, user data or tools are exported.
        if self.resultado is not None:
            raise FalhaStreaming("evento após resultado")
        sid = self.identidade(event.get("sessionId"))
        if self.session_id is not None and sid != self.session_id:
            return  # Child sessions have independent identities.
        turn = self.identidade(event.get("turnId"))
        if tipo != "turn.started" and turn != self.turn_id:
            return
        seq = event.get("seq")
        if type(seq) is not int or seq < 0:
            raise FalhaStreaming("sequência nativa inválida")
        digest = hashlib.sha256(raw.encode("utf-8")).digest()
        if seq == self.seq and digest == self.digest:
            return
        if seq <= self.seq:
            raise FalhaStreaming("sequência nativa reescrita")
        self.seq, self.digest = seq, digest
        if tipo == "turn.started":
            if not self.turno_completo or not self.completo:
                raise FalhaStreaming("turno reiniciado antes de terminar")
            self.turnos += 1
            if self.turnos > 256:
                raise FalhaStreaming("limite de turnos excedido")
            self.session_id, self.turn_id = sid, turn
            self.turno_completo = False
            return
        if tipo == "turn.failed":
            raise FalhaStreaming("turno nativo falhou")
        if tipo == "turn.completed":
            if not self.completo:
                raise FalhaStreaming("turno terminou com mensagem incompleta")
            self.turno_completo = True
            return
        if tipo == "streamRecovery.updated":
            if not self.completo and self.texto.ultimo_texto:
                raise FalhaStreaming("recuperação nativa após texto parcial")
            if not self.completo:
                self.message_id = None
            self.completo = True
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise FalhaStreaming("evento nativo inválido")
        kind = payload.get("kind")
        if tipo == "tool.updated":
            if not self.texto.liberado:
                return
            if kind in ("started", "result", "error"):
                identity = self.identidade(payload.get("toolCallId"))
                if kind == "started":
                    if len(self.tools) >= 256:
                        raise FalhaStreaming("limite de ferramentas excedido")
                    name = str(payload.get("toolName") or "ferramenta").replace("\n", " ")[:80]
                    self.tools[identity] = name
                    self.progresso.evento("▶ " + name)
                else:
                    name = self.tools.pop(identity, None)
                    if name is not None:
                        self.progresso.evento(("✔ " if kind == "result" else "✖ ") + name)
            return
        if kind == "error":
            raise FalhaStreaming("geração nativa falhou")
        mid = self.identidade(payload.get("assistantMessageId"))
        if kind == "start":
            if not self.completo or mid == self.message_id:
                raise FalhaStreaming("mensagem nativa reiniciada")
            self.verificar(sid)
            self.texto.liberar()
            self.texto.abrir_segmento(mid)
            self.message_id, self.completo = mid, False
            self.reasoning_chars = 0
            return
        if mid != self.message_id or self.completo:
            raise FalhaStreaming("delta sem início de mensagem")
        if kind == "finish":
            if payload.get("done") is not True:
                raise FalhaStreaming("fim nativo inválido")
            self.completo = True
        elif kind in ("text_delta", "reasoning_delta"):
            delta = payload.get("delta")
            self.texto._validar_texto(delta)
            if kind == "text_delta":
                self.texto.delta(delta)
            else:
                restante = max(0, 1500 - self.reasoning_chars)
                if restante:
                    self.progresso.texto(delta[:restante])
                    if len(delta) > restante:
                        self.progresso.texto("…")
                self.reasoning_chars += len(delta)
        # Tool payloads and non-text framing have no public content.
