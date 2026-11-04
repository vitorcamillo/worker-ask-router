#!/usr/bin/env python3
"""Guarda do Bonsai: GameMode tem prioridade absoluta.

Dois comandos:
- `check`: consulta ao vivo o ClientCount do GameMode via DBus e o lease do
  Whisper. Livre sai 0; GameMode ativo, Whisper usando a GPU ou falha de
  consulta sai 75 com mensagem em stderr para o wrapper descer ao próximo
  provedor (fail closed).
- `serve`: proxy HTTP em 127.0.0.1:10115 para o modelo em 127.0.0.1:10116.
  Toda request autenticada consulta a disponibilidade (GameMode primeiro,
  depois lease do Whisper) ANTES de encaminhar; indisponível responde 503
  sem tocar no upstream. Um monitor DBus em
  thread daemon para `bonsai-llama.service` (argv fixo) quando o GameMode
  fica ativo; nunca reinicia o modelo e nunca toca em outros serviços.

Stdlib + Gio, sem dependências pip. A chave de API é lida apenas em runtime
(no `serve`), nunca logada. Nenhuma opção de runtime desliga a regra.
"""

from __future__ import annotations

import hmac
import http.client
import json
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

GUARD_HOST = "127.0.0.1"
GUARD_PORT = 10115
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 10116
UPSTREAM_TIMEOUT_S = 180.0
DBUS_TIMEOUT_S = 1.0
POLL_INTERVAL_S = 1
MAX_BODY_BYTES = 1024 * 1024  # 1 MiB
HEALTH_TIMEOUT_S = 5.0  # readiness não pode pendurar o wrapper
EXIT_UNAVAILABLE = 75
API_KEY_PATH = Path.home() / ".local" / "share" / "bonsai" / "api-key"
STOP_MODEL_ARGV = ["systemctl", "--user", "stop", "bonsai-llama.service"]

GAMEMODE_NAME = "com.feralinteractive.GameMode"
GAMEMODE_PATH = "/com/feralinteractive/GameMode"
GAMEMODE_IFACE = "com.feralinteractive.GameMode"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

MSG_CHECK_ACTIVE = (
    "BONSAI_UNAVAILABLE: GameMode ativo; use o próximo modelo/provedor. "
    "Nenhuma chamada ao Bonsai foi feita."
)
MSG_CHECK_UNKNOWN = (
    "BONSAI_UNAVAILABLE: estado do GameMode desconhecido; "
    "use o próximo modelo/provedor."
)
MSG_CHECK_WHISPER = (
    "BONSAI_UNAVAILABLE: Whisper está usando a GPU; "
    "use o próximo modelo/provedor."
)
MSG_GATEWAY_ACTIVE = (
    "GameMode ativo; use o próximo modelo/provedor. "
    "Nenhuma chamada ao Bonsai foi feita."
)
MSG_GATEWAY_UNKNOWN = (
    "Estado do GameMode desconhecido; use o próximo modelo/provedor."
)
MSG_GATEWAY_WHISPER = (
    "Whisper está usando a GPU; use o próximo modelo/provedor."
)
MSG_SERVER_STOPPED = (
    "Servidor Bonsai parado; use o próximo modelo/provedor "
    "ou inicie-o com `bonsai-server start`."
)
MSG_SERVER_LOADING = (
    "Servidor Bonsai ainda carregando; aguarde e tente novamente."
)

# /health é readiness real, sem auth: GameMode inativo E upstream /health 200.
OPEN_PATHS = {"/health"}
AUTH_PATHS = {
    "/v1/models",
    "/props",
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/responses/input_tokens",
    "/v1/messages",
    "/v1/messages/count_tokens",
}

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
DROP_REQUEST_HEADERS = HOP_BY_HOP | {
    "host",
    "content-length",
    "cookie",
    "accept-encoding",
}


def _log(message: str) -> None:
    sys.stderr.write(f"guard: {message}\n")
    sys.stderr.flush()


def dbus_client_count(timeout_s: float = DBUS_TIMEOUT_S):
    """Consulta ClientCount ao vivo; retorna (count, ok). Falha => (None, False)."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = bus.call_sync(
            GAMEMODE_NAME,
            GAMEMODE_PATH,
            PROPERTIES_IFACE,
            "Get",
            GLib.Variant("(ss)", (GAMEMODE_IFACE, "ClientCount")),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            int(timeout_s * 1000),
            None,
        )
        return int(result.unpack()[0]), True
    except Exception:
        return None, False


def check_gamemode(querier=None):
    """Ponto de reuso (wrappers/testes, sem ler segredos).

    Retorna ("ok", 0), ("active", n) ou ("unknown", None). Exceção do
    querier ou count inválido (None, negativo, não-int) vira "unknown":
    fail closed.
    """
    try:
        count, ok = (querier or dbus_client_count)()
    except Exception:
        return "unknown", None
    if (
        not ok
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
    ):
        return "unknown", None  # count impossível é desconhecido, nunca "ok"
    if count > 0:
        return "active", count
    return "ok", 0


def check_availability(querier=None):
    """Disponibilidade completa para wrappers/gateway: jogo primeiro.

    Retorna ("ok", 0), ("active", n), ("unknown", None) ou ("whisper", None).
    O GameMode é consultado sempre; só com jogo inativo o lease do Whisper é
    olhado. Falha ao ler o estado do Whisper é fail closed ("whisper").
    """
    status, count = check_gamemode(querier)
    if status != "ok":
        return status, count
    import gpu_coord  # tardio: gpu_coord importa game_guard só dentro do lease

    try:
        busy = gpu_coord.whisper_busy()
    except Exception:
        busy = True
    return ("whisper", None) if busy else ("ok", 0)


def stop_model_service() -> bool:
    """Para APENAS o modelo; argv fixo, sem shell, timeout 10 s."""
    try:
        done = subprocess.run(
            STOP_MODEL_ARGV,
            timeout=10.0,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


class GameModeMonitor:
    """Para o modelo quando o GameMode fica ativo (ou desconhecido).

    `evaluate()` é chamado ao iniciar, a cada sinal DBus da interface e da
    propriedade, e pelo timer periódico (cobre perda de sinal). Não existe
    caminho de start: GameMode desativando não reinicia nada.
    """

    def __init__(self, querier=None, stopper=None):
        self._querier = querier or dbus_client_count
        self._stopper = stopper or stop_model_service
        self._stopped_ok = False

    def evaluate(self) -> bool:
        status, _count = check_gamemode(self._querier)
        # Repetir a parada cobre um start no intervalo curto entre launcher e jogo.
        self._stopped_ok = False
        if status == "ok":
            self._stopped_ok = False
            return False
        if not self._stopped_ok:
            try:
                self._stopped_ok = bool(self._stopper())
            except Exception:
                self._stopped_ok = False  # stopper falhou: tenta no próximo poll
        return True

    def run(self) -> None:
        loop = GLib.MainLoop.new(None, False)
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            for iface in (GAMEMODE_IFACE, PROPERTIES_IFACE):
                bus.signal_subscribe(
                    GAMEMODE_NAME,
                    iface,
                    None,
                    GAMEMODE_PATH,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    self._on_signal,
                )
        except Exception as exc:
            _log(
                f"monitor sem sinais DBus ({getattr(exc, 'message', exc)}); "
                "segue com polling"
            )
        self.evaluate()
        GLib.timeout_add_seconds(POLL_INTERVAL_S, self._on_timer)
        loop.run()

    def _on_signal(self, *args):
        self.evaluate()

    def _on_timer(self):
        self.evaluate()
        return GLib.SOURCE_CONTINUE


def start_monitor(monitor: GameModeMonitor | None = None) -> GameModeMonitor:
    mon = monitor or GameModeMonitor()
    threading.Thread(
        target=mon.run, name="bonsai-guard-monitor", daemon=True
    ).start()
    return mon


class GuardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 75  # conexão do cliente ociosa não segura a thread para sempre
    _headers_sent = False

    def do_GET(self):
        self._process("GET")

    def do_POST(self):
        self._process("POST")

    def _process(self, method: str) -> None:
        self._headers_sent = False
        try:
            self._route(method)
        except OSError:
            # cliente caiu no meio da resposta: sem traceback
            self.close_connection = True
        except Exception:
            _log(f"erro interno em {method} -> 500")
            if not self._headers_sent:
                self._send_guard_error(
                    500, "bonsai_guard_error", "Erro interno da guarda."
                )
            else:
                self.close_connection = True

    def _route(self, method: str) -> None:
        path = self._path()
        if path not in OPEN_PATHS and path not in AUTH_PATHS:
            self._send_guard_error(
                404, "bonsai_route_not_allowed", "Rota não permitida pela guarda."
            )
            return
        if path in OPEN_PATHS:
            self._health()
            return
        credential = self._credential()
        if credential is None:
            self._send_guard_error(
                401, "bonsai_unauthorized", "Token ausente ou inválido."
            )
            return
        # GameMode e lease do Whisper ao vivo a cada request: fecha a janela
        # entre sinal/polling e a liberação do lease.
        if self._availability_blocked():
            return
        self._forward(method, credential)

    def _availability_blocked(self) -> bool:
        """True (com 503 já enviado) se indisponível neste instante."""
        status, _count = check_availability(self.server.querier)
        if status == "unknown":
            self._send_guard_error(503, "bonsai_gamemode_unknown", MSG_GATEWAY_UNKNOWN)
            return True
        if status == "active":
            self._send_guard_error(503, "bonsai_gamemode_active", MSG_GATEWAY_ACTIVE)
            return True
        if status == "whisper":
            self._send_guard_error(503, "bonsai_whisper_active", MSG_GATEWAY_WHISPER)
            return True
        return False

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _credential(self):
        """Retorna (header, valor) validado ou None. O valor nunca é logado."""
        expected = self.server.token
        candidates = []
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            candidates.append(("Authorization", auth[len("Bearer "):].strip()))
        candidates.append(("x-api-key", self.headers.get("x-api-key", "").strip()))
        for name, given in candidates:
            if hmac.compare_digest(
                given.encode("utf-8"), expected.encode("utf-8")
            ):
                return name, given
        return None

    def _health(self) -> None:
        """Readiness real para wrappers/E2E: 200 só se utilizável agora.

        GameMode ativo/desconhecido, Whisper usando a GPU, upstream carregando
        (não-200) ou parado respondem 503. Nunca ecoa token nem corpo do
        upstream: saúde é só saúde.
        """
        status, _count = check_availability(self.server.querier)
        if status != "ok":
            if status == "active":
                self._send_guard_error(503, "bonsai_gamemode_active", MSG_GATEWAY_ACTIVE)
            elif status == "whisper":
                self._send_guard_error(503, "bonsai_whisper_active", MSG_GATEWAY_WHISPER)
            else:
                self._send_guard_error(503, "bonsai_gamemode_unknown", MSG_GATEWAY_UNKNOWN)
            return
        conn = http.client.HTTPConnection(
            self.server.upstream_host,
            self.server.upstream_port,
            timeout=HEALTH_TIMEOUT_S,
        )
        try:
            conn.request(
                "GET",
                "/health",
                headers={"Authorization": f"Bearer {self.server.token}"},
            )
            response = conn.getresponse()
            response.read()
            upstream_ok = response.status == 200
        except (OSError, http.client.HTTPException):
            self._send_guard_error(503, "bonsai_server_stopped", MSG_SERVER_STOPPED)
            return
        finally:
            conn.close()
        if not upstream_ok:
            self._send_guard_error(503, "bonsai_server_loading", MSG_SERVER_LOADING)
            return
        payload = json.dumps(
            {"status": "ok", "service": "bonsai-guard"}
        ).encode("utf-8")
        self._headers_sent = True
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _forward(self, method: str, credential) -> None:
        body = None
        if method == "POST":
            body = self._read_body()
            if body is None:
                return  # erro já respondido
            # A leitura acima bloqueia: GameMode/Whisper pode ter ativado
            # durante a espera. Revalida já com o corpo em mãos, imediatamente
            # antes de tocar no upstream.
            if self._availability_blocked():
                return
        conn = http.client.HTTPConnection(
            self.server.upstream_host,
            self.server.upstream_port,
            timeout=UPSTREAM_TIMEOUT_S,
        )
        try:
            conn.request(
                method, self.path, body=body, headers=self._upstream_headers(credential)
            )
            response = conn.getresponse()
        except (OSError, http.client.HTTPException):
            conn.close()
            self._send_guard_error(503, "bonsai_server_stopped", MSG_SERVER_STOPPED)
            return
        try:
            self._relay(response)
        finally:
            conn.close()

    def _read_body(self):
        # Framing estrito: qualquer Transfer-Encoding, Content-Length
        # duplicado/inválido ou corpo acima do limite é rejeitado sem ler o
        # corpo (a resposta fecha a conexão, sem request desync).
        if "Transfer-Encoding" in self.headers:
            self._send_guard_error(
                400, "bonsai_bad_request", "Transfer-Encoding não é aceito."
            )
            return None
        lengths = self.headers.get_all("Content-Length") or []
        if not lengths:
            self._send_guard_error(
                411, "bonsai_length_required", "Content-Length é obrigatório no POST."
            )
            return None
        if len(lengths) > 1:
            self._send_guard_error(
                400, "bonsai_bad_request", "Content-Length duplicado."
            )
            return None
        try:
            length = int(lengths[0])
        except ValueError:
            length = -1
        if length < 0:
            self._send_guard_error(400, "bonsai_bad_request", "Content-Length inválido.")
            return None
        if length > MAX_BODY_BYTES:
            self._send_guard_error(
                413,
                "bonsai_body_too_large",
                f"Corpo excede o limite de {MAX_BODY_BYTES} bytes.",
            )
            return None
        return self.rfile.read(length)

    def _upstream_headers(self, credential):
        out = {}
        for name, value in self.headers.items():
            if name.lower() in DROP_REQUEST_HEADERS:
                continue
            if name.lower() in ("authorization", "x-api-key"):
                continue  # só o credential validado segue
            out[name] = value
        name, value = credential
        if name == "Authorization":
            # _credential valida o valor cru; o upstream espera o scheme.
            value = f"Bearer {value}"
        out[name] = value
        return out

    def _relay(self, response: http.client.HTTPResponse) -> None:
        """Repassa status/Content-Type e flui o corpo sem buffering completo."""
        self._headers_sent = True
        self.send_response(response.status)
        content_type = response.getheader("Content-Type")
        if content_type:
            self.send_header("Content-Type", content_type)
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            self.send_header("Content-Length", content_length)
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        try:
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            self.close_connection = True

    def _send_guard_error(self, code: int, slug: str, message: str) -> None:
        self._headers_sent = True
        # Rejeição sem consumir o corpo: fecha a conexão para evitar desync.
        self.close_connection = True
        payload = json.dumps(
            {"error": {"code": slug, "message": message, "type": "bonsai_guard_error"}}
        ).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")  # corpo não lido não polui keep-alive
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        return  # silencioso: nunca logar payload, token ou query string

    def log_request(self, code="-", size="-"):
        path = self._path()
        if path not in OPEN_PATHS and path not in AUTH_PATHS:
            path = "-"  # nunca ecoar path arbitrário do cliente
        _log(f"{self.command} {path} -> {code}")


class GuardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, token, querier, upstream_host, upstream_port):
        super().__init__(address, GuardHandler)
        self.token = token
        self.querier = querier
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):
            return  # desconexão de cliente: normal, sem traceback
        _log(f"conexão encerrada com erro: {type(exc).__name__}")


def make_server(
    *,
    token,
    querier=None,
    host: str = GUARD_HOST,
    port: int = GUARD_PORT,
    upstream_host: str = UPSTREAM_HOST,
    upstream_port: int = UPSTREAM_PORT,
) -> GuardServer:
    """Fábrica injetável (testes usam token/querier/upstream próprios)."""
    return GuardServer(
        (host, port),
        token=token,
        querier=querier or dbus_client_count,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
    )


def read_api_key(path: Path = API_KEY_PATH) -> str:
    """Lê a chave em runtime; o valor nunca é impresso nem logado."""
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise SystemExit(f"guard: não foi possível ler a chave de API em {path}")
    if not key:
        raise SystemExit(f"guard: chave de API vazia em {path}")
    return key


def serve() -> int:
    token = read_api_key()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # saída limpa: sem restart
    try:
        server = make_server(token=token)
    except OSError as exc:
        raise SystemExit(
            f"guard: não foi possível escutar em {GUARD_HOST}:{GUARD_PORT} ({exc})"
        )
    start_monitor()
    _log(f"guarda em http://{GUARD_HOST}:{GUARD_PORT} -> {UPSTREAM_HOST}:{UPSTREAM_PORT}")
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
    return 0


USAGE = (
    "uso: game_guard.py check   consulta o GameMode (exit 0 ok, 75 indisponível)\n"
    "     game_guard.py serve    proxy 127.0.0.1:10115 -> 127.0.0.1:10116 com monitor\n"
)


def main(argv=None, *, querier=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("check", "serve"):
        sys.stderr.write(USAGE)
        return 2
    if args[0] == "check":
        status, _count = check_availability(querier)
        if status == "ok":
            return 0
        mensagem = {
            "active": MSG_CHECK_ACTIVE,
            "whisper": MSG_CHECK_WHISPER,
        }.get(status, MSG_CHECK_UNKNOWN)
        sys.stderr.write(mensagem + "\n")
        return EXIT_UNAVAILABLE
    return serve()


if __name__ == "__main__":
    sys.exit(main())
