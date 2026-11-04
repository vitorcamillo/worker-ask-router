"""Testes offline da guarda GameMode.

Nenhum DBus real, nenhum segredo real e nenhum acesso externo: GameMode e
upstream são falsos injetados; a guarda e o upstream falso escutam só em
portas efêmeras de loopback. Rodar com: python3 -m unittest discover -s tests -v
"""

import contextlib
import http.client
import inspect
import io
import json
import socket
import subprocess
import sys
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import game_guard as gg  # noqa: E402

TOKEN = "chave-de-teste-sintetica"
MSG_ATIVO_EXATA = (
    "BONSAI_UNAVAILABLE: GameMode ativo; use o próximo modelo/provedor. "
    "Nenhuma chamada ao Bonsai foi feita."
)


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._reply()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._reply()

    def _reply(self):
        state = self.server
        state.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "x-api-key": self.headers.get("x-api-key"),
                "cookie": self.headers.get("Cookie"),
            }
        )
        if state.mode == "loading":
            body = json.dumps({"carregando": True}).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if state.mode == "sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: ev0\n\n")
            self.wfile.flush()
            state.t_ev0 = time.monotonic()
            time.sleep(0.2)  # janela para o cliente ler ev0 antes de ev1
            state.t_ev1 = time.monotonic()
            self.wfile.write(b"data: ev1\n\n")
            self.wfile.flush()
            self.wfile.write(b"data: fim\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


class FakeUpstream:
    def __init__(self, mode="json"):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)
        self.httpd.mode = mode
        self.httpd.requests = []
        self.httpd.t_ev0 = 0.0
        self.httpd.t_ev1 = float("inf")
        self.port = self.httpd.server_address[1]
        self._stopped = False
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def requests(self):
        return self.httpd.requests

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self.httpd.shutdown()
        self.httpd.server_close()


class GuardaHttpTeste(unittest.TestCase):
    """Sobe a guarda com token/querier/upstream injetados (nada real)."""

    modo_upstream = "json"

    def fazer_querier(self):
        return lambda: (0, True)  # GameMode inativo por padrão

    def setUp(self):
        self.upstream = FakeUpstream(mode=self.modo_upstream)
        self.addCleanup(self.upstream.stop)
        # Whisper livre por padrão: testes não podem depender do estado real.
        livre = unittest.mock.patch("gpu_coord.whisper_busy", return_value=False)
        livre.start()
        self.addCleanup(livre.stop)
        self.guard = gg.make_server(
            token=TOKEN,
            querier=self.fazer_querier(),
            port=0,
            upstream_port=self.upstream.port,
        )
        self.guard_port = self.guard.server_address[1]
        threading.Thread(target=self.guard.serve_forever, daemon=True).start()
        self.addCleanup(self.guard.server_close)
        self.addCleanup(self.guard.shutdown)

    def requisicao(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.guard_port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            dados = resp.read()
            cabec = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, cabec, dados
        finally:
            conn.close()

    def requisicao_bruta(self, header_lines, payload=b""):
        """Request com headers controlados byte a byte (putrequest)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.guard_port, timeout=10)
        try:
            conn.putrequest("POST", "/v1/chat/completions", skip_accept_encoding=True)
            for nome, valor in header_lines:
                conn.putheader(nome, valor)
            conn.endheaders()
            if payload:
                conn.send(payload)
            resp = conn.getresponse()
            dados = resp.read()
            status = resp.status
        finally:
            conn.close()
        return status, dados

    def autenticado(self, **extra):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        headers.update(extra)
        return headers


class TestCheckGamemode(unittest.TestCase):
    def test_client_count_zero_e_ok(self):
        self.assertEqual(gg.check_gamemode(lambda: (0, True)), ("ok", 0))

    def test_client_count_positivo_e_ativo(self):
        self.assertEqual(gg.check_gamemode(lambda: (2, True)), ("active", 2))

    def test_falha_de_consulta_e_desconhecido(self):
        self.assertEqual(gg.check_gamemode(lambda: (None, False)), ("unknown", None))

    def test_excecao_do_querier_e_fail_closed(self):
        def explosivo():
            raise RuntimeError("dbus caiu")

        self.assertEqual(gg.check_gamemode(explosivo), ("unknown", None))

    def test_count_none_negativo_ou_nao_int_e_fail_closed(self):
        for count in (None, -1, "2", 2.5, True):
            with self.subTest(count=count):
                self.assertEqual(
                    gg.check_gamemode(lambda c=count: (c, True)), ("unknown", None)
                )


class TestCliCheck(unittest.TestCase):
    def executar(self, argv, querier, whisper_busy=False):
        with unittest.mock.patch("gpu_coord.whisper_busy", return_value=whisper_busy):
            erro = io.StringIO()
            with contextlib.redirect_stderr(erro):
                codigo = gg.main(argv, querier=querier)
        return codigo, erro.getvalue()

    def test_client_count_zero_exit_0(self):
        codigo, _ = self.executar(["check"], lambda: (0, True))
        self.assertEqual(codigo, 0)

    def test_client_count_1_exit_75_com_mensagem_exata(self):
        codigo, erro = self.executar(["check"], lambda: (1, True))
        self.assertEqual(codigo, 75)
        self.assertEqual(erro.strip(), MSG_ATIVO_EXATA)

    def test_falha_dbus_exit_75(self):
        codigo, erro = self.executar(["check"], lambda: (None, False))
        self.assertEqual(codigo, 75)
        self.assertIn("BONSAI_UNAVAILABLE", erro)
        self.assertIn("próximo modelo/provedor", erro)

    def test_whisper_ocupado_exit_75(self):
        codigo, erro = self.executar(["check"], lambda: (0, True), whisper_busy=True)
        self.assertEqual(codigo, 75)
        self.assertIn("BONSAI_UNAVAILABLE", erro)
        self.assertIn("Whisper", erro)
        self.assertIn("próximo modelo/provedor", erro)

    def test_gamemode_tem_prioridade_sobre_whisper(self):
        codigo, erro = self.executar(["check"], lambda: (1, True), whisper_busy=True)
        self.assertEqual(codigo, 75)
        self.assertEqual(erro.strip(), MSG_ATIVO_EXATA)

    def test_flag_de_bypass_nao_existe(self):
        codigo, _ = self.executar(["--desligar-guarda"], lambda: (0, True))
        self.assertEqual(codigo, 2)

    def test_modulo_nao_le_ambiente(self):
        fonte = inspect.getsource(gg)
        self.assertNotIn("environ", fonte)


class TestCheckAvailability(unittest.TestCase):
    def test_gamemode_ativo_e_checado_antes_do_whisper(self):
        with unittest.mock.patch("gpu_coord.whisper_busy") as busy:
            self.assertEqual(gg.check_availability(lambda: (2, True)), ("active", 2))
        busy.assert_not_called()

    def test_gamemode_desconhecido_e_checado_antes_do_whisper(self):
        with unittest.mock.patch("gpu_coord.whisper_busy") as busy:
            self.assertEqual(
                gg.check_availability(lambda: (None, False)), ("unknown", None)
            )
        busy.assert_not_called()

    def test_jogo_livre_e_whisper_ocupado(self):
        with unittest.mock.patch("gpu_coord.whisper_busy", return_value=True):
            self.assertEqual(
                gg.check_availability(lambda: (0, True)), ("whisper", None)
            )

    def test_jogo_livre_e_whisper_livre_e_ok(self):
        with unittest.mock.patch("gpu_coord.whisper_busy", return_value=False):
            self.assertEqual(gg.check_availability(lambda: (0, True)), ("ok", 0))

    def test_erro_no_whisper_e_fail_closed(self):
        with unittest.mock.patch(
            "gpu_coord.whisper_busy", side_effect=RuntimeError("lock sumiu")
        ):
            self.assertEqual(
                gg.check_availability(lambda: (0, True)), ("whisper", None)
            )


class TestAutorizacao(GuardaHttpTeste):
    def test_sem_token_401_e_upstream_nao_chamado(self):
        status, _, _ = self.requisicao("POST", "/v1/chat/completions", body=b"{}")
        self.assertEqual(status, 401)
        self.assertEqual(self.upstream.requests, [])

    def test_token_errado_401_sem_upstream(self):
        status, _, dados = self.requisicao(
            "POST",
            "/v1/chat/completions",
            body=b"{}",
            headers={"Authorization": "Bearer token-errado"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(dados)["error"]["code"], "bonsai_unauthorized")
        self.assertEqual(self.upstream.requests, [])

    def test_bearer_valido_encaminha_o_mesmo_token(self):
        status, _, _ = self.requisicao(
            "POST", "/v1/chat/completions", body=b"{}", headers=self.autenticado()
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.upstream.requests[0]["authorization"], f"Bearer {TOKEN}"
        )

    def test_x_api_key_valido_encaminha(self):
        status, _, _ = self.requisicao(
            "POST",
            "/v1/messages",
            body=b"{}",
            headers={"x-api-key": TOKEN, "anthropic-version": "2023-06-01"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.requests[0]["x-api-key"], TOKEN)

    def test_cookie_nao_e_encaminhado(self):
        self.requisicao(
            "POST",
            "/v1/chat/completions",
            body=b"{}",
            headers=self.autenticado(Cookie="sessao=SEGREDO-DE-COOKIE"),
        )
        self.assertIsNone(self.upstream.requests[0]["cookie"])


class TestGameModeAtivo(GuardaHttpTeste):
    def fazer_querier(self):
        return lambda: (1, True)

    def test_ativo_503_com_code_e_nenhuma_chamada_upstream(self):
        status, _, dados = self.requisicao(
            "POST", "/v1/chat/completions", body=b"{}", headers=self.autenticado()
        )
        self.assertEqual(status, 503)
        erro = json.loads(dados)["error"]
        self.assertEqual(erro["code"], "bonsai_gamemode_active")
        self.assertIn("próximo modelo/provedor", erro["message"])
        self.assertEqual(self.upstream.requests, [])

    def test_ativo_tambem_bloqueia_models_e_count_tokens(self):
        for rota, metodo in (
            ("/v1/models", "GET"),
            ("/v1/messages/count_tokens", "POST"),
            ("/v1/responses/input_tokens", "POST"),
        ):
            with self.subTest(rota=rota):
                status, _, dados = self.requisicao(
                    metodo,
                    rota,
                    body=b"{}" if metodo == "POST" else None,
                    headers=self.autenticado(),
                )
                self.assertEqual(status, 503)
                self.assertEqual(
                    json.loads(dados)["error"]["code"], "bonsai_gamemode_active"
                )
        self.assertEqual(self.upstream.requests, [])


class TestGameModeDesconhecido(GuardaHttpTeste):
    def fazer_querier(self):
        def quebrado():
            raise RuntimeError("sessão dbus caiu")

        return quebrado

    def test_consulta_falha_503_gamemode_unknown_fail_closed(self):
        status, _, dados = self.requisicao(
            "GET", "/v1/models", headers=self.autenticado()
        )
        self.assertEqual(status, 503)
        erro = json.loads(dados)["error"]
        self.assertEqual(erro["code"], "bonsai_gamemode_unknown")
        self.assertIn("próximo modelo/provedor", erro["message"])
        self.assertEqual(self.upstream.requests, [])

    def test_health_desconhecido_503_fail_closed(self):
        status, _, dados = self.requisicao("GET", "/health")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(dados)["error"]["code"], "bonsai_gamemode_unknown")
        self.assertEqual(self.upstream.requests, [])


class TestHealthReadiness(GuardaHttpTeste):
    """/health é readiness real: só 200 com GameMode inativo e upstream 200."""

    def test_pronto_200_e_consulta_upstream_com_token_local(self):
        status, _, dados = self.requisicao("GET", "/health")
        self.assertEqual(status, 200)
        corpo = json.loads(dados)
        self.assertEqual(corpo["status"], "ok")
        self.assertNotIn(TOKEN.encode(), dados)
        req = self.upstream.requests[0]
        self.assertEqual(req["path"], "/health")
        self.assertEqual(req["authorization"], f"Bearer {TOKEN}")

    def test_health_nao_expoe_detalhes_so_saude(self):
        _, _, dados = self.requisicao("GET", "/health")
        self.assertEqual(set(json.loads(dados)), {"status", "service"})


class TestHealthGamemodeAtivo(GuardaHttpTeste):
    def fazer_querier(self):
        return lambda: (1, True)

    def test_health_ativo_503_sem_tocar_upstream(self):
        status, _, dados = self.requisicao("GET", "/health")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(dados)["error"]["code"], "bonsai_gamemode_active")
        self.assertEqual(self.upstream.requests, [])


class TestWhisperOcupado(GuardaHttpTeste):
    """Jogo livre, mas lease do Whisper segurando a GPU."""

    def fazer_querier(self):
        return lambda: (0, True)

    def setUp(self):
        super().setUp()
        ocupado = unittest.mock.patch("gpu_coord.whisper_busy", return_value=True)
        ocupado.start()
        self.addCleanup(ocupado.stop)

    def test_post_503_bonsai_whisper_active_sem_upstream(self):
        status, _, dados = self.requisicao(
            "POST", "/v1/chat/completions", body=b"{}", headers=self.autenticado()
        )
        self.assertEqual(status, 503)
        erro = json.loads(dados)["error"]
        self.assertEqual(erro["code"], "bonsai_whisper_active")
        self.assertIn("Whisper", erro["message"])
        self.assertIn("próximo modelo/provedor", erro["message"])
        self.assertEqual(self.upstream.requests, [])

    def test_health_503_bonsai_whisper_active_sem_upstream(self):
        status, _, dados = self.requisicao("GET", "/health")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(dados)["error"]["code"], "bonsai_whisper_active")
        self.assertEqual(self.upstream.requests, [])

    def test_sem_token_401_antes_da_checagem_do_whisper(self):
        status, _, _ = self.requisicao("POST", "/v1/chat/completions", body=b"{}")
        self.assertEqual(status, 401)


class TestHealthCarregando(GuardaHttpTeste):
    modo_upstream = "loading"

    def test_health_upstream_nao_200_vira_503_carregando(self):
        status, _, dados = self.requisicao("GET", "/health")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(dados)["error"]["code"], "bonsai_server_loading")
        self.assertNotIn(TOKEN.encode(), dados)


class TestRotasEBody(GuardaHttpTeste):
    def test_rota_fora_da_allowlist_404_sem_upstream(self):
        status, _, dados = self.requisicao(
            "GET", "/v1/completions", headers=self.autenticado()
        )
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(dados)["error"]["code"], "bonsai_route_not_allowed")
        self.assertEqual(self.upstream.requests, [])

    def test_metodo_nao_suportado_nao_encaminha(self):
        status, _, _ = self.requisicao(
            "DELETE", "/v1/chat/completions", headers=self.autenticado()
        )
        self.assertIn(status, (401, 405, 501))
        self.assertEqual(self.upstream.requests, [])

    def test_body_acima_do_limite_413_sem_drain_e_com_close(self):
        # Declara 2 MiB mas manda só 64 KiB: a guarda tem que responder 413
        # e fechar, sem tentar drenar o corpo declarado.
        sock = socket.create_connection(("127.0.0.1", self.guard_port), timeout=10)
        try:
            cabecalho = (
                "POST /v1/chat/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.guard_port}\r\n"
                f"Authorization: Bearer {TOKEN}\r\n"
                f"Content-Length: {2 * 1024 * 1024}\r\n"
                "\r\n"
            ).encode("ascii")
            sock.sendall(cabecalho + b"x" * 65536)
            dados = b""
            try:
                while b"bonsai_body_too_large" not in dados:
                    bloco = sock.recv(65536)
                    if not bloco:
                        break
                    dados += bloco
            except OSError:
                pass
        finally:
            sock.close()
        self.assertIn(b"HTTP/1.1 413", dados)
        self.assertIn(b"bonsai_body_too_large", dados)
        self.assertIn(b"Connection: close", dados)
        self.assertEqual(self.upstream.requests, [])

    def test_log_de_rota_invalida_nao_ecoa_o_path(self):
        erro = io.StringIO()
        with contextlib.redirect_stderr(erro):
            self.requisicao(
                "GET",
                "/v1/completions?token=SEGREDO-NO-QUERY",
                headers=self.autenticado(),
            )
        saida = erro.getvalue()
        self.assertNotIn("/v1/completions", saida)
        self.assertNotIn("SEGREDO-NO-QUERY", saida)

    def test_log_de_rota_permitida_mostra_path_e_status(self):
        erro = io.StringIO()
        with contextlib.redirect_stderr(erro):
            self.requisicao("GET", "/health")
        self.assertIn("GET /health -> 200", erro.getvalue())

    def test_rotas_de_count_tokens_estao_na_allowlist(self):
        self.assertIn("/v1/messages/count_tokens", gg.AUTH_PATHS)
        self.assertIn("/v1/responses/input_tokens", gg.AUTH_PATHS)


class TestRevalidacaoAposCorpoGameMode(GuardaHttpTeste):
    """GameMode ativa entre a 1ª checagem e o fim da leitura do corpo."""

    def fazer_querier(self):
        chamadas = {"n": 0}

        def querier():
            chamadas["n"] += 1
            # 1ª consulta (antes do corpo): livre. Da 2ª em diante: ativo.
            return (0, True) if chamadas["n"] == 1 else (1, True)

        return querier

    def test_post_bloqueado_na_revalidacao_sem_upstream(self):
        status, _, dados = self.requisicao(
            "POST", "/v1/chat/completions", body=b"{}", headers=self.autenticado()
        )
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(dados)["error"]["code"], "bonsai_gamemode_active")
        self.assertEqual(self.upstream.requests, [])


class TestRevalidacaoAposCorpoWhisper(GuardaHttpTeste):
    """Lease do Whisper surge entre a 1ª checagem e o fim da leitura."""

    def fazer_querier(self):
        return lambda: (0, True)

    def setUp(self):
        super().setUp()
        chamadas = {"n": 0}

        def busy():
            chamadas["n"] += 1
            return chamadas["n"] > 1

        ocupado = unittest.mock.patch("gpu_coord.whisper_busy", side_effect=busy)
        ocupado.start()
        self.addCleanup(ocupado.stop)

    def test_post_bloqueado_na_revalidacao_sem_upstream(self):
        status, _, dados = self.requisicao(
            "POST", "/v1/chat/completions", body=b"{}", headers=self.autenticado()
        )
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(dados)["error"]["code"], "bonsai_whisper_active")
        self.assertEqual(self.upstream.requests, [])


class TestFraming(GuardaHttpTeste):
    def test_transfer_encoding_qualquer_400_sem_upstream(self):
        status, dados = self.requisicao_bruta(
            [
                ("Authorization", f"Bearer {TOKEN}"),
                ("Transfer-Encoding", "chunked"),
            ],
            payload=b"0\r\n\r\n",
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(dados)["error"]["code"], "bonsai_bad_request")
        self.assertEqual(self.upstream.requests, [])

    def test_content_length_duplicado_400_sem_upstream(self):
        status, dados = self.requisicao_bruta(
            [
                ("Authorization", f"Bearer {TOKEN}"),
                ("Content-Length", "2"),
                ("Content-Length", "2"),
            ],
            payload=b"{}",
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(dados)["error"]["code"], "bonsai_bad_request")
        self.assertEqual(self.upstream.requests, [])

    def test_content_length_invalido_400_sem_upstream(self):
        status, dados = self.requisicao_bruta(
            [
                ("Authorization", f"Bearer {TOKEN}"),
                ("Content-Length", "dois"),
            ],
            payload=b"{}",
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(dados)["error"]["code"], "bonsai_bad_request")
        self.assertEqual(self.upstream.requests, [])


class TestModeloParado(GuardaHttpTeste):
    def setUp(self):
        super().setUp()
        self.upstream.stop()  # porta fecha: conexões são recusadas

    def test_upstream_parado_503_bonsai_server_stopped(self):
        status, _, dados = self.requisicao(
            "POST", "/v1/chat/completions", body=b"{}", headers=self.autenticado()
        )
        self.assertEqual(status, 503)
        erro = json.loads(dados)["error"]
        self.assertEqual(erro["code"], "bonsai_server_stopped")
        self.assertIn("bonsai-server start", erro["message"])

    def test_health_com_upstream_parado_503(self):
        status, _, dados = self.requisicao("GET", "/health")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(dados)["error"]["code"], "bonsai_server_stopped")


class TestStreaming(GuardaHttpTeste):
    modo_upstream = "sse"

    def test_sse_flui_sem_buffering_e_preserva_content_type(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.guard_port, timeout=10)
        try:
            conn.request(
                "POST", "/v1/chat/completions", body=b"{}", headers=self.autenticado()
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
            self.assertIsNone(resp.getheader("Content-Length"))
            self.assertEqual(resp.getheader("Connection"), "close")
            primeiro = resp.read1(64)
            t_primeiro = time.monotonic()
            self.assertTrue(primeiro.startswith(b"data: ev0"))
            resto = b""
            while True:
                bloco = resp.read1(64)
                if not bloco:
                    break
                resto += bloco
        finally:
            conn.close()
        # ev0 chegou no cliente antes de ev1 ser emitido: nada foi bufferizado
        self.assertLess(t_primeiro, self.upstream.httpd.t_ev1)
        self.assertEqual(primeiro + resto, b"data: ev0\n\ndata: ev1\n\ndata: fim\n\n")


class StopperFalso:
    def __init__(self):
        self.chamadas = 0

    def __call__(self):
        self.chamadas += 1
        return True


class TestMonitor(unittest.TestCase):
    def test_stop_tem_argv_fixo_somente_do_modelo(self):
        self.assertEqual(
            gg.STOP_MODEL_ARGV,
            ["systemctl", "--user", "stop", "bonsai-llama.service"],
        )

    def test_stop_model_service_usa_argv_fixo_e_timeout_10(self):
        with unittest.mock.patch("subprocess.run") as fake_run:
            fake_run.return_value = unittest.mock.Mock(returncode=0)
            self.assertTrue(gg.stop_model_service())
        fake_run.assert_called_once()
        args, kwargs = fake_run.call_args
        self.assertEqual(args[0], gg.STOP_MODEL_ARGV)
        self.assertEqual(kwargs.get("timeout"), 10.0)
        self.assertIs(kwargs.get("check"), False)

    def test_stop_model_service_timeout_ou_erro_retorna_false(self):
        with unittest.mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=10),
        ):
            self.assertFalse(gg.stop_model_service())
        with unittest.mock.patch("subprocess.run", side_effect=OSError("sem systemctl")):
            self.assertFalse(gg.stop_model_service())

    def test_monitor_padrao_para_apenas_o_modelo(self):
        monitor = gg.GameModeMonitor(querier=lambda: (0, True))
        self.assertIs(monitor._stopper, gg.stop_model_service)

    def test_estado_ativo_repete_stop_mesmo_sem_observar_intervalo_inativo(self):
        stopper = StopperFalso()
        monitor = gg.GameModeMonitor(querier=lambda: (1, True), stopper=stopper)
        self.assertTrue(monitor.evaluate())
        monitor.evaluate()
        self.assertEqual(stopper.chamadas, 2)

    def test_estado_inativo_nao_para_nem_starta(self):
        stopper = StopperFalso()
        monitor = gg.GameModeMonitor(querier=lambda: (0, True), stopper=stopper)
        self.assertFalse(monitor.evaluate())
        monitor.evaluate()
        self.assertEqual(stopper.chamadas, 0)

    def test_erro_de_consulta_para_o_modelo_fail_closed(self):
        stopper = StopperFalso()

        def quebrado():
            raise RuntimeError("dbus caiu")

        monitor = gg.GameModeMonitor(querier=quebrado, stopper=stopper)
        self.assertTrue(monitor.evaluate())
        self.assertEqual(stopper.chamadas, 1)

    def test_stop_falho_tenta_de_novo_na_proxima_avaliacao(self):
        chamadas = []
        resultado = {"ok": False}

        def stopper():
            chamadas.append(1)
            return resultado["ok"]

        monitor = gg.GameModeMonitor(querier=lambda: (1, True), stopper=stopper)
        self.assertTrue(monitor.evaluate())
        resultado["ok"] = True
        monitor.evaluate()
        self.assertEqual(len(chamadas), 2)

    def test_voltar_a_inativo_e_ativar_de_novo_para_de_novo(self):
        stopper = StopperFalso()
        estado = {"count": 0}
        monitor = gg.GameModeMonitor(
            querier=lambda: (estado["count"], True), stopper=stopper
        )
        monitor.evaluate()
        self.assertEqual(stopper.chamadas, 0)
        estado["count"] = 1
        monitor.evaluate()
        self.assertEqual(stopper.chamadas, 1)
        estado["count"] = 0  # desativou: nada acontece (sem reinício do modelo)
        monitor.evaluate()
        self.assertEqual(stopper.chamadas, 1)
        estado["count"] = 1
        monitor.evaluate()
        self.assertEqual(stopper.chamadas, 2)

    def test_count_invalido_para_o_modelo_fail_closed(self):
        for count in (None, -1, "2"):
            with self.subTest(count=count):
                stopper = StopperFalso()
                monitor = gg.GameModeMonitor(
                    querier=lambda c=count: (c, True), stopper=stopper
                )
                self.assertTrue(monitor.evaluate())
                self.assertEqual(stopper.chamadas, 1)

    def test_stopper_explodindo_nao_derruba_o_poll(self):
        tentativas = {"n": 0}

        def stopper_ruim():
            tentativas["n"] += 1
            raise OSError("systemctl desapareceu")

        monitor = gg.GameModeMonitor(querier=lambda: (1, True), stopper=stopper_ruim)
        self.assertTrue(monitor.evaluate())
        self.assertTrue(monitor.evaluate())  # segue tentando a cada poll
        self.assertEqual(tentativas["n"], 2)
        monitor._stopper = StopperFalso()
        self.assertTrue(monitor.evaluate())
        self.assertEqual(monitor._stopper.chamadas, 1)


if __name__ == "__main__":
    unittest.main()
