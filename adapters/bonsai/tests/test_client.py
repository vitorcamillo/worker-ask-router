"""Testes offline do cliente bonsai-ask.

Nenhum acesso a rede ou ao serviço real: `urlopen` e o caminho da chave são
injetados. Rodar com: python3 -m unittest discover -s tests -v
"""

import contextlib
import http.server
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bonsai_client  # noqa: E402

VALID_KEY = "chave-de-teste-sintetica"
SECRET_BODY = '{"erro":"CORPO-HTTP-SECRETO-DO-TESTE"}'
FIXED_URL = "http://127.0.0.1:10115/v1/chat/completions"


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class BoundedBody(io.BytesIO):
    def __init__(self, payload: bytes):
        super().__init__(payload)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


def resposta_chat(content="Resposta curta.", finish_reason="stop", reasoning=None):
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return json.dumps(
        {"choices": [{"message": message, "finish_reason": finish_reason}]}
    ).encode("utf-8")


def urlopen_ok(payload: bytes, capture: dict = None):
    def _urlopen(request, timeout=None):
        if capture is not None:
            capture["request"] = request
            capture["timeout"] = timeout
        return FakeResponse(payload)

    return _urlopen


def urlopen_erro(exc):
    def _urlopen(request, timeout=None):
        raise exc

    return _urlopen


class BaseClienteTest(unittest.TestCase):
    def chave_temporaria(self, conteudo=VALID_KEY):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        caminho = Path(tmp.name) / "api-key"
        caminho.write_text(conteudo + "\n", encoding="utf-8")
        return caminho

    def executar_main(self, argv, *, stdin=None, urlopen_fn=None, key_path=None):
        saida, erro = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(erro):
            codigo = bonsai_client.main(
                argv,
                stdin=stdin if stdin is not None else io.StringIO(""),
                key_path=key_path,
                urlopen=urlopen_fn,
            )
        return codigo, saida.getvalue(), erro.getvalue()


class TestConsultaFeliz(BaseClienteTest):
    def test_retorna_apenas_content(self):
        conteudo = bonsai_client.query(
            "Diga ok",
            key_path=self.chave_temporaria(),
            urlopen=urlopen_ok(resposta_chat("ok", reasoning="RACIOCINIO-OCULTO")),
        )
        self.assertEqual(conteudo, "ok")
        self.assertNotIn("RACIOCINIO", conteudo)

    def test_requisicao_tem_payload_e_headers_corretos(self):
        capture = {}
        bonsai_client.query(
            "Diga ok",
            key_path=self.chave_temporaria(),
            urlopen=urlopen_ok(resposta_chat("ok"), capture=capture),
        )
        request = capture["request"]
        self.assertEqual(capture["timeout"], 180)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, FIXED_URL)
        dados = json.loads(request.data.decode("utf-8"))
        self.assertEqual(dados["model"], "bonsai-2-27b")
        self.assertIs(dados["stream"], False)
        self.assertEqual(dados["temperature"], 0.7)
        self.assertEqual(dados["max_tokens"], 2048)
        self.assertEqual(dados["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(dados["messages"][0]["role"], "system")
        self.assertTrue(dados["messages"][0]["content"].strip())
        self.assertEqual(dados["messages"][1], {"role": "user", "content": "Diga ok"})
        self.assertEqual(request.headers["Authorization"], f"Bearer {VALID_KEY}")
        self.assertEqual(request.headers["Content-type"], "application/json")

    def test_main_imprime_so_content_com_reasoning_presente(self):
        codigo, saida, erro = self.executar_main(
            ["Diga ok"],
            urlopen_fn=urlopen_ok(resposta_chat("ok", reasoning="RACIOCINIO-OCULTO")),
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 0)
        self.assertEqual(saida, "ok\n")
        self.assertNotIn("RACIOCINIO", saida + erro)

    def test_timeout_customizado_e_passo_ao_urlopen(self):
        capture = {}
        bonsai_client.query(
            "Diga ok",
            timeout=300,
            max_tokens=4096,
            key_path=self.chave_temporaria(),
            urlopen=urlopen_ok(resposta_chat("ok"), capture=capture),
        )
        self.assertEqual(capture["timeout"], 300)
        dados = json.loads(capture["request"].data.decode("utf-8"))
        self.assertEqual(dados["max_tokens"], 4096)

    def test_url_e_fixa_e_sem_flag_de_url(self):
        self.assertEqual(bonsai_client.BASE_URL, FIXED_URL)
        ajuda = bonsai_client.build_arg_parser().format_help()
        self.assertNotIn("--url", ajuda)
        self.assertNotIn("--base-url", ajuda)


class TestValidacaoPrompt(BaseClienteTest):
    def test_prompt_vazio_recusado(self):
        with self.assertRaises(bonsai_client.ClientError) as ctx:
            bonsai_client.query(
                "",
                key_path=self.chave_temporaria(),
                urlopen=urlopen_ok(resposta_chat("ok")),
            )
        self.assertIn("vazio", str(ctx.exception))

    def test_prompt_somente_espacos_recusado(self):
        with self.assertRaises(bonsai_client.ClientError):
            bonsai_client.query(
                "   \n\t ",
                key_path=self.chave_temporaria(),
                urlopen=urlopen_ok(resposta_chat("ok")),
            )

    def test_prompt_vazio_nao_envia_requisicao(self):
        chamou = []

        def espiao(request, timeout=None):
            chamou.append(True)
            return FakeResponse(resposta_chat("ok"))

        with self.assertRaises(bonsai_client.ClientError):
            bonsai_client.query(
                "",
                key_path=self.chave_temporaria(),
                urlopen=espiao,
            )
        self.assertFalse(chamou)

    def test_limite_de_65536_bytes_aceito(self):
        prompt = "é" * 32768  # 2 bytes por caractere = 65536 bytes
        capture = {}
        conteudo = bonsai_client.query(
            prompt,
            key_path=self.chave_temporaria(),
            urlopen=urlopen_ok(resposta_chat("ok"), capture=capture),
        )
        self.assertEqual(conteudo, "ok")
        enviado = json.loads(capture["request"].data.decode("utf-8"))
        self.assertEqual(enviado["messages"][1]["content"], prompt)

    def test_65537_bytes_recusado(self):
        prompt = "é" * 32768 + "x"  # 65537 bytes
        with self.assertRaises(bonsai_client.ClientError) as ctx:
            bonsai_client.query(
                prompt,
                key_path=self.chave_temporaria(),
                urlopen=urlopen_ok(resposta_chat("ok")),
            )
        self.assertIn("excede", str(ctx.exception))

    def test_limite_conta_bytes_nao_caracteres(self):
        # "あ" ocupa 3 bytes: 21846 caracteres são 65538 bytes.
        prompt = "あ" * 21846
        self.assertLess(len(prompt), bonsai_client.MAX_PROMPT_BYTES)
        with self.assertRaises(bonsai_client.ClientError):
            bonsai_client.query(
                prompt,
                key_path=self.chave_temporaria(),
                urlopen=urlopen_ok(resposta_chat("ok")),
            )


class TestChaveApi(BaseClienteTest):
    def test_chave_ausente_erro_claro(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(bonsai_client.ClientError) as ctx:
            bonsai_client.query(
                "Diga ok",
                key_path=Path(tmp.name) / "api-key",
                urlopen=urlopen_ok(resposta_chat("ok")),
            )
        self.assertIn("Chave", str(ctx.exception))

    def test_chave_vazia_erro(self):
        with self.assertRaises(bonsai_client.ClientError):
            bonsai_client.query(
                "Diga ok",
                key_path=self.chave_temporaria(conteudo="   \n"),
                urlopen=urlopen_ok(resposta_chat("ok")),
            )

    def test_chave_com_espacos_vai_limpa_no_header(self):
        capture = {}
        bonsai_client.query(
            "Diga ok",
            key_path=self.chave_temporaria(conteudo="  chave-com-espacos  \n"),
            urlopen=urlopen_ok(resposta_chat("ok"), capture=capture),
        )
        self.assertEqual(
            capture["request"].headers["Authorization"], "Bearer chave-com-espacos"
        )

    def test_erro_de_chave_nao_expoe_valor(self):
        valor = "VALOR-SECRETO-QUE-NAO-PODE-SAIR"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        caminho = Path(tmp.name) / "api-key"
        caminho.write_text(valor + "\n", encoding="utf-8")
        caminho.chmod(0o000)
        try:
            caminho.read_text(encoding="utf-8")
        except OSError:
            pass
        else:
            self.skipTest(
                "ambiente consegue ler arquivo com modo 000 (root); "
                "teste de permissão não se aplica aqui"
            )
        with self.assertRaises(bonsai_client.ClientError) as ctx:
            bonsai_client.query(
                "Diga ok", key_path=caminho, urlopen=urlopen_ok(resposta_chat("ok"))
            )
        self.assertNotIn(valor, str(ctx.exception))


class TestErrosHttpETimeout(BaseClienteTest):
    def test_http_503_codigos_gateway_sao_exit_75_com_mensagem_explicita(self):
        casos = {
            "bonsai_gamemode_active": "GameMode ativo",
            "bonsai_gamemode_unknown": "estado do GameMode desconhecido",
            "bonsai_server_stopped": "servidor Bonsai parado",
        }
        for codigo_gateway, trecho in casos.items():
            with self.subTest(codigo_gateway=codigo_gateway):
                corpo = BoundedBody(
                    json.dumps(
                        {
                            "error": {
                                "code": codigo_gateway,
                                "detail": "DETALHE-SECRETO-DO-GATEWAY",
                            }
                        }
                    ).encode("utf-8")
                )
                erro_http = urllib.error.HTTPError(
                    FIXED_URL, 503, "Service Unavailable", None, corpo
                )
                codigo, saida, erro = self.executar_main(
                    ["Diga ok"],
                    urlopen_fn=urlopen_erro(erro_http),
                    key_path=self.chave_temporaria(),
                )
                self.assertEqual(codigo, 75)
                self.assertEqual(saida, "")
                self.assertIn(trecho, erro)
                self.assertIn("próximo modelo", erro)
                self.assertNotIn("DETALHE-SECRETO", erro)
                self.assertNotIn(VALID_KEY, erro)
                self.assertEqual(corpo.read_sizes, [8192])

    def test_http_503_malformado_ou_codigo_desconhecido_e_generico_sem_vazamento(self):
        corpos = [
            BoundedBody(b"nao-json-DETALHE-SECRETO-DO-GATEWAY"),
            BoundedBody(
                b'{"error":{"code":"outro_codigo",'
                b'"detail":"DETALHE-SECRETO-DO-GATEWAY"}}'
            ),
        ]
        for corpo in corpos:
            with self.subTest(corpo=corpo.getvalue()):
                erro_http = urllib.error.HTTPError(
                    FIXED_URL, 503, "Service Unavailable", None, corpo
                )
                with self.assertRaises(bonsai_client.ClientUnavailable) as ctx:
                    bonsai_client.query(
                        "Diga ok",
                        key_path=self.chave_temporaria(),
                        urlopen=urlopen_erro(erro_http),
                    )
                mensagem = str(ctx.exception)
                self.assertIn("gateway Bonsai indisponível", mensagem)
                self.assertNotIn("DETALHE-SECRETO", mensagem)
                self.assertNotIn("outro_codigo", mensagem)
                self.assertEqual(corpo.read_sizes, [8192])

    def test_http_503_corpo_maior_e_lido_com_limite(self):
        corpo = BoundedBody(b"x" * 8192 + b"DETALHE-SECRETO-ALEM-DO-LIMITE")
        erro_http = urllib.error.HTTPError(
            FIXED_URL, 503, "Service Unavailable", None, corpo
        )
        with self.assertRaises(bonsai_client.ClientUnavailable):
            bonsai_client.query(
                "Diga ok",
                key_path=self.chave_temporaria(),
                urlopen=urlopen_erro(erro_http),
            )
        self.assertEqual(corpo.read_sizes, [8192])

    def test_timeout_direto_gera_erro_claro(self):
        with self.assertRaises(bonsai_client.ClientError) as ctx:
            bonsai_client.query(
                "Diga ok",
                timeout=7,
                key_path=self.chave_temporaria(),
                urlopen=urlopen_erro(TimeoutError("timed out")),
            )
        self.assertIn("Tempo limite", str(ctx.exception))
        self.assertIn("7s", str(ctx.exception))

    def test_urlerror_com_reason_timeout(self):
        erro = urllib.error.URLError(TimeoutError("timed out"))
        with self.assertRaises(bonsai_client.ClientError) as ctx:
            bonsai_client.query(
                "Diga ok",
                key_path=self.chave_temporaria(),
                urlopen=urlopen_erro(erro),
            )
        self.assertIn("Tempo limite", str(ctx.exception))

    def test_conexao_recusada_orienta_bonsai_server_start(self):
        erro = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        with self.assertRaises(bonsai_client.ClientError) as ctx:
            bonsai_client.query(
                "Diga ok",
                key_path=self.chave_temporaria(),
                urlopen=urlopen_erro(erro),
            )
        self.assertIn("bonsai-server start", str(ctx.exception))

    def test_oserror_de_rede_tambem_orienta_start(self):
        with self.assertRaises(bonsai_client.ClientError) as ctx:
            bonsai_client.query(
                "Diga ok",
                key_path=self.chave_temporaria(),
                urlopen=urlopen_erro(ConnectionResetError()),
            )
        self.assertIn("bonsai-server start", str(ctx.exception))

    def test_http_401_sem_vazar_corpo_ou_chave(self):
        erro = urllib.error.HTTPError(
            FIXED_URL, 401, "Unauthorized", None, io.BytesIO(SECRET_BODY.encode())
        )
        with self.assertRaises(bonsai_client.ClientError) as ctx:
            bonsai_client.query(
                "Diga ok",
                key_path=self.chave_temporaria(),
                urlopen=urlopen_erro(erro),
            )
        mensagem = str(ctx.exception)
        self.assertIn("401", mensagem)
        self.assertNotIn(SECRET_BODY, mensagem)
        self.assertNotIn(VALID_KEY, mensagem)

    def test_http_500_sem_vazar_corpo_ou_chave(self):
        erro = urllib.error.HTTPError(
            FIXED_URL, 500, "Internal Server Error", None, io.BytesIO(SECRET_BODY.encode())
        )
        with self.assertRaises(bonsai_client.ClientError) as ctx:
            bonsai_client.query(
                "Diga ok",
                key_path=self.chave_temporaria(),
                urlopen=urlopen_erro(erro),
            )
        mensagem = str(ctx.exception)
        self.assertIn("500", mensagem)
        self.assertNotIn(SECRET_BODY, mensagem)
        self.assertNotIn(VALID_KEY, mensagem)


class TestParsingResposta(BaseClienteTest):
    def consultar(self, payload: bytes):
        return bonsai_client.query(
            "Diga ok",
            key_path=self.chave_temporaria(),
            urlopen=urlopen_ok(payload),
        )

    def test_content_vazio_com_stop_e_erro(self):
        with self.assertRaises(bonsai_client.ClientError):
            self.consultar(resposta_chat(content="", finish_reason="stop"))

    def test_content_none_e_erro(self):
        with self.assertRaises(bonsai_client.ClientError):
            self.consultar(resposta_chat(content=None))

    def test_length_sem_content_e_erro_claro(self):
        with self.assertRaises(bonsai_client.ClientError) as ctx:
            self.consultar(resposta_chat(content="", finish_reason="length"))
        mensagem = str(ctx.exception)
        self.assertIn("length", mensagem)
        self.assertIn("--max-tokens", mensagem)

    def test_reasoning_sem_content_nao_vaza_raciocinio(self):
        with self.assertRaises(bonsai_client.ClientError) as ctx:
            self.consultar(resposta_chat(content=None, reasoning="RACIOCINIO-OCULTO"))
        self.assertNotIn("RACIOCINIO", str(ctx.exception))

    def test_sem_choices_e_erro(self):
        with self.assertRaises(bonsai_client.ClientError):
            self.consultar(b'{"objet": "estranho"}')

    def test_choices_vazio_e_erro(self):
        with self.assertRaises(bonsai_client.ClientError):
            self.consultar(b'{"choices": []}')

    def test_json_nao_objeto_e_erro(self):
        with self.assertRaises(bonsai_client.ClientError):
            self.consultar(b"[1, 2, 3]")

    def test_json_invalido_e_erro(self):
        with self.assertRaises(bonsai_client.ClientError):
            self.consultar(b"nao-sou-json")

    def test_sem_message_e_erro(self):
        with self.assertRaises(bonsai_client.ClientError):
            self.consultar(b'{"choices": [{"finish_reason": "stop"}]}')


class TestParametros(BaseClienteTest):
    def test_timeout_limites_validos(self):
        bonsai_client.validate_params(timeout=1, max_tokens=2048)
        bonsai_client.validate_params(timeout=600, max_tokens=2048)

    def test_timeout_fora_da_faixa(self):
        for valor in (0, 601, -5):
            with self.assertRaises(bonsai_client.ClientError):
                bonsai_client.validate_params(timeout=valor, max_tokens=2048)

    def test_max_tokens_limites_validos(self):
        bonsai_client.validate_params(timeout=180, max_tokens=1)
        bonsai_client.validate_params(timeout=180, max_tokens=4096)

    def test_max_tokens_fora_da_faixa(self):
        for valor in (0, 4097, -1):
            with self.assertRaises(bonsai_client.ClientError):
                bonsai_client.validate_params(timeout=180, max_tokens=valor)

    def test_main_com_timeout_invalido_exit_1_sem_requisicao(self):
        chamou = []

        def espiao(request, timeout=None):
            chamou.append(True)
            return FakeResponse(resposta_chat("ok"))

        codigo, _, erro = self.executar_main(
            ["--timeout", "0", "Diga ok"],
            urlopen_fn=espiao,
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 1)
        self.assertFalse(chamou)
        self.assertIn("timeout", erro.lower())

    def test_main_com_max_tokens_invalido_exit_1(self):
        codigo, _, erro = self.executar_main(
            ["--max-tokens", "4097", "Diga ok"],
            urlopen_fn=urlopen_ok(resposta_chat("ok")),
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 1)
        self.assertIn("max-tokens", erro)


class TestCli(BaseClienteTest):
    def test_main_com_prompt_argumento(self):
        codigo, saida, erro = self.executar_main(
            ["Diga ok"],
            urlopen_fn=urlopen_ok(resposta_chat("ok")),
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 0)
        self.assertEqual(saida, "ok\n")
        self.assertEqual(erro, "")

    def test_main_le_stdin_quando_sem_prompt(self):
        capture = {}
        codigo, saida, _ = self.executar_main(
            [],
            stdin=io.StringIO("Pergunta pela stdin"),
            urlopen_fn=urlopen_ok(resposta_chat("ok"), capture=capture),
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 0)
        self.assertEqual(saida, "ok\n")
        enviado = json.loads(capture["request"].data.decode("utf-8"))
        self.assertEqual(enviado["messages"][1]["content"], "Pergunta pela stdin")

    def test_main_le_arquivo_quando_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        arquivo = Path(tmp.name) / "pergunta.txt"
        arquivo.write_text("Pergunta via arquivo", encoding="utf-8")
        capture = {}
        codigo, saida, _ = self.executar_main(
            ["--file", str(arquivo)],
            urlopen_fn=urlopen_ok(resposta_chat("ok"), capture=capture),
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 0)
        self.assertEqual(saida, "ok\n")
        enviado = json.loads(capture["request"].data.decode("utf-8"))
        self.assertEqual(enviado["messages"][1]["content"], "Pergunta via arquivo")

    def test_main_file_vazio_recusado(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        arquivo = Path(tmp.name) / "vazio.txt"
        arquivo.write_text("", encoding="utf-8")
        codigo, _, erro = self.executar_main(
            ["--file", str(arquivo)],
            urlopen_fn=urlopen_ok(resposta_chat("ok")),
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 1)
        self.assertIn("vazio", erro)

    def test_main_file_inexistente_exit_1_sem_traceback(self):
        codigo, _, erro = self.executar_main(
            ["--file", "/caminho/inexistente/pergunta.txt"],
            urlopen_fn=urlopen_ok(resposta_chat("ok")),
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 1)
        self.assertIn("erro:", erro)
        self.assertNotIn("Traceback", erro)

    def test_main_file_e_argumento_sao_exclusivos(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        arquivo = Path(tmp.name) / "pergunta.txt"
        arquivo.write_text("Pergunta via arquivo", encoding="utf-8")
        codigo, _, erro = self.executar_main(
            ["--file", str(arquivo), "Pergunta por argumento"],
            urlopen_fn=urlopen_ok(resposta_chat("ok")),
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 1)
        self.assertIn("origem", erro)

    def test_main_prompt_vazio_recusado(self):
        codigo, _, erro = self.executar_main(
            [""],
            urlopen_fn=urlopen_ok(resposta_chat("ok")),
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 1)
        self.assertIn("vazio", erro)

    def test_main_sem_prompt_e_stdin_tty_recusa(self):
        class TtyFalso(io.StringIO):
            def isatty(self):
                return True

        codigo, _, erro = self.executar_main(
            [],
            stdin=TtyFalso(),
            urlopen_fn=urlopen_ok(resposta_chat("ok")),
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 1)
        self.assertIn("Nenhum prompt", erro)

    def test_main_indisponivel_orienta_start_sem_traceback(self):
        erro_rede = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        codigo, _, erro = self.executar_main(
            ["Diga ok"],
            urlopen_fn=urlopen_erro(erro_rede),
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 75)
        self.assertIn("bonsai-server start", erro)
        self.assertNotIn("Traceback", erro)

    def test_main_http_erro_nao_vaza_corpo_nem_chave_nem_traceback(self):
        erro_http = urllib.error.HTTPError(
            FIXED_URL, 401, "Unauthorized", None, io.BytesIO(SECRET_BODY.encode())
        )
        codigo, _, erro = self.executar_main(
            ["Diga ok"],
            urlopen_fn=urlopen_erro(erro_http),
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 1)
        self.assertNotIn(SECRET_BODY, erro)
        self.assertNotIn(VALID_KEY, erro)
        self.assertNotIn("Traceback", erro)

    def test_main_excecao_inesperada_nao_vaza_detalhe_nem_traceback(self):
        def explosivo(request, timeout=None):
            raise RuntimeError("DETALHE-SECRETO-DO-BUG")

        codigo, _, erro = self.executar_main(
            ["Diga ok"],
            urlopen_fn=explosivo,
            key_path=self.chave_temporaria(),
        )
        self.assertEqual(codigo, 1)
        self.assertNotIn("DETALHE-SECRETO-DO-BUG", erro)
        self.assertNotIn("Traceback", erro)

    def test_main_argumento_nao_numerico_exit_2(self):
        with self.assertRaises(SystemExit) as ctx:
            self.executar_main(
                ["--timeout", "abc", "Diga ok"],
                urlopen_fn=urlopen_ok(resposta_chat("ok")),
                key_path=self.chave_temporaria(),
            )
        self.assertEqual(ctx.exception.code, 2)


class TestSemRedirecionamento(BaseClienteTest):
    def test_handler_recusa_redirect(self):
        handler = bonsai_client._NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(
                None, None, 302, "Found", {}, "http://127.0.0.1:9/evil"
            )
        )

    def test_redirect_nao_envia_autorizacao_ao_destino(self):
        destino_hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                if self.path == "/orig":
                    porta = self.server.server_address[1]
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{porta}/evil")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._destino()

            def do_GET(self):
                # Transporte vulnerável reenvia o POST como GET ao destino do 3xx.
                self._destino()

            def _destino(self):
                destino_hits.append(self.headers.get("Authorization"))
                corpo = resposta_chat("ok")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def log_message(self, fmt, *args):
                return

        servidor = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        porta = servidor.server_address[1]
        threading.Thread(target=servidor.serve_forever, daemon=True).start()
        self.addCleanup(servidor.server_close)
        self.addCleanup(servidor.shutdown)
        with unittest.mock.patch.object(
            bonsai_client, "BASE_URL", f"http://127.0.0.1:{porta}/orig"
        ):
            with self.assertRaises(bonsai_client.ClientError) as ctx:
                # Sem urlopen injetado: o transporte padrão não segue o 3xx.
                bonsai_client.query("Diga ok", key_path=self.chave_temporaria())
        self.assertEqual(destino_hits, [])
        self.assertNotIn(VALID_KEY, str(ctx.exception))

    def test_proxy_do_ambiente_nao_recebe_prompt_nem_chave(self):
        alvo_hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                alvo_hits.append(self.headers.get("Authorization"))
                corpo = resposta_chat("ok")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def log_message(self, fmt, *args):
                return

        servidor = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        porta = servidor.server_address[1]
        threading.Thread(target=servidor.serve_forever, daemon=True).start()
        self.addCleanup(servidor.server_close)
        self.addCleanup(servidor.shutdown)

        # "Proxy" local que apenas registra o que receber: se o transporte
        # herdasse http_proxy do ambiente, prompt e Bearer chegariam aqui.
        proxy_hits = []
        proxy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        proxy.bind(("127.0.0.1", 0))
        proxy.listen(4)
        porta_proxy = proxy.getsockname()[1]

        def escuta_proxy():
            try:
                while True:
                    conn, _ = proxy.accept()
                    with conn:
                        proxy_hits.append(conn.recv(8192))
            except OSError:
                pass

        threading.Thread(target=escuta_proxy, daemon=True).start()
        self.addCleanup(proxy.close)
        with unittest.mock.patch.object(
            bonsai_client, "BASE_URL", f"http://127.0.0.1:{porta}/v1/chat/completions"
        ):
            with unittest.mock.patch.dict(
                os.environ,
                {
                    "http_proxy": f"http://127.0.0.1:{porta_proxy}",
                    "HTTP_PROXY": f"http://127.0.0.1:{porta_proxy}",
                    "no_proxy": "",
                    "NO_PROXY": "",
                },
            ):
                # Sem urlopen injetado: o transporte padrão ignora o proxy do ambiente.
                conteudo = bonsai_client.query(
                    "Diga ok", timeout=5, key_path=self.chave_temporaria()
                )
        self.assertEqual(conteudo, "ok")
        self.assertEqual(alvo_hits, [f"Bearer {VALID_KEY}"])
        self.assertEqual(proxy_hits, [])


class TestReadPromptDireto(BaseClienteTest):
    def test_origens_na_ordem(self):
        self.assertEqual(bonsai_client.read_prompt("arg", None), "arg")

        class TtyFalso(io.StringIO):
            def isatty(self):
                return True

        with self.assertRaises(bonsai_client.ClientError):
            bonsai_client.read_prompt(None, None, stdin=TtyFalso())


if __name__ == "__main__":
    unittest.main()
