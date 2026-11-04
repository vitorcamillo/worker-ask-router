#!/usr/bin/env python3
"""Cliente do serviço local Bonsai (subagente textual).

Chamado pelo wrapper `bonsai-ask` como script comum. Envia um prompt ao
serviço loopback autenticado em http://127.0.0.1:10115/v1/chat/completions
e imprime SOMENTE o conteúdo da resposta (nunca raciocínio, corpo HTTP,
cabeçalhos ou segredos).

Contrato:
- Sem prompt e sem --file, lê o texto da stdin. Arquivo só via --file;
  nenhum acesso automático ao diretório corrente.
- Chave lida de ~/.local/share/bonsai/api-key e enviada como Bearer.
- Não executa código retornado e não faz auto-start: se o serviço estiver
  parado, orienta `bonsai-server start`.
- URL fixa, nunca configurável pelo usuário.
- Redirecionamentos HTTP são recusados e proxies do ambiente (http_proxy e
  afins) são ignorados: a chave Bearer nunca sai do loopback, nem seguindo um
  3xx para outro host nem via proxy.
- Limites: timeout 1..600 s, max_tokens 1..4096, prompt até 65536 bytes
  UTF-8 (checados antes de enviar); prompt vazio é recusado.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

BASE_URL = "http://127.0.0.1:10115/v1/chat/completions"
MODEL = "bonsai-2-27b"
DEFAULT_KEY_PATH = Path.home() / ".local" / "share" / "bonsai" / "api-key"
DEFAULT_TIMEOUT = 180
MAX_TIMEOUT = 600
DEFAULT_MAX_TOKENS = 2048
MAX_TOKENS_LIMIT = 4096
MAX_PROMPT_BYTES = 65536
MAX_GATEWAY_ERROR_BODY_BYTES = 8192
EXIT_UNAVAILABLE = 75
SYSTEM_PROMPT = "Você é um assistente local. Responda em PT-BR, direto ao ponto."

GATEWAY_ERROR_MESSAGES = {
    "bonsai_whisper_active": "BONSAI_UNAVAILABLE: Whisper está usando a GPU; use o próximo modelo/provedor.",
    "bonsai_gamemode_active": (
        "BONSAI_UNAVAILABLE: GameMode ativo; use o próximo modelo/provedor. "
        "Nenhuma chamada ao Bonsai foi feita."
    ),
    "bonsai_gamemode_unknown": (
        "BONSAI_UNAVAILABLE: estado do GameMode desconhecido; "
        "use o próximo modelo/provedor."
    ),
    "bonsai_server_stopped": (
        "BONSAI_UNAVAILABLE: servidor Bonsai parado; use o próximo modelo/provedor "
        "ou inicie-o com `bonsai-server start`."
    ),
}

Urlopen = Callable[..., object]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Recusa 3xx: devolver None faz o opener levantar HTTPError sem
    reenviar a requisição (e o Authorization) ao destino do Location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_no_redirect(request, timeout=None):
    """urlopen sem redirects e sem proxy do ambiente; transporte padrão de `query`/`main`.

    ProxyHandler({}) substitui o handler padrão: sem ele, `build_opener` herdaria
    http_proxy/HTTPS_PROXY do ambiente e enviaria prompt e Bearer a um proxy remoto.
    """
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirectHandler()
    )
    return opener.open(request, timeout=timeout)


class ClientError(Exception):
    """Erro esperado do cliente: vira mensagem em stderr com exit 1."""


class ClientUnavailable(ClientError):
    """Indisponibilidade temporária: vira mensagem em stderr com exit 75."""


def load_key(key_path: Path = DEFAULT_KEY_PATH) -> str:
    """Lê a chave de API do caminho fixo; nunca imprime nem loga o valor."""
    try:
        text = key_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ClientError(
            f"Chave de API não encontrada em {key_path}. "
            "Gere a chave (orquestrador/bonsai-server) antes de consultar."
        )
    except OSError:
        raise ClientError(f"Não foi possível ler a chave de API em {key_path}.")
    key = text.strip()
    if not key:
        raise ClientError(f"A chave de API em {key_path} está vazia.")
    return key


def validate_params(*, timeout: int, max_tokens: int) -> None:
    if not 1 <= timeout <= MAX_TIMEOUT:
        raise ClientError(
            f"--timeout inválido: use entre 1 e {MAX_TIMEOUT} segundos (recebido {timeout})."
        )
    if not 1 <= max_tokens <= MAX_TOKENS_LIMIT:
        raise ClientError(
            f"--max-tokens inválido: use entre 1 e {MAX_TOKENS_LIMIT} (recebido {max_tokens})."
        )


def validate_prompt(prompt: str) -> None:
    if not prompt or not prompt.strip():
        raise ClientError(
            "Prompt vazio: passe um argumento, use --file ARQUIVO ou envie texto pela stdin."
        )
    size = len(prompt.encode("utf-8"))
    if size > MAX_PROMPT_BYTES:
        raise ClientError(
            f"Prompt tem {size} bytes UTF-8 e excede o limite de {MAX_PROMPT_BYTES} bytes."
        )


def build_payload(prompt: str, max_tokens: int) -> dict:
    return {
        "model": MODEL,
        "stream": False,
        "temperature": 0.7,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }


def extract_content(data: object) -> str:
    """Extrai só `choices[0].message.content`; ignora reasoning e o resto."""
    if not isinstance(data, dict):
        raise ClientError("Resposta malformada: era esperado um objeto JSON.")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ClientError("Resposta malformada: nenhuma 'choices' na resposta.")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ClientError("Resposta malformada: item de 'choices' inesperado.")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ClientError("Resposta malformada: sem 'message' na resposta.")
    content = message.get("content")
    if not isinstance(content, str) or not content:
        if choice.get("finish_reason") == "length":
            raise ClientError(
                "Resposta cortada pelo limite de tokens (finish_reason=length) e sem "
                "conteúdo. Aumente --max-tokens."
            )
        raise ClientError("O serviço não retornou conteúdo na resposta.")
    return content


def parse_response(raw: bytes) -> str:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ClientError("Resposta malformada: o serviço não devolveu JSON válido.")
    return extract_content(data)


def query(
    prompt: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    key_path: Path = DEFAULT_KEY_PATH,
    urlopen: Urlopen = _open_no_redirect,
) -> str:
    """Consulta o serviço; injetável em `urlopen` e `key_path` para testes."""
    validate_params(timeout=timeout, max_tokens=max_tokens)
    validate_prompt(prompt)
    key = load_key(key_path)
    request = urllib.request.Request(
        BASE_URL,
        data=json.dumps(build_payload(prompt, max_tokens)).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ClientError(
                "Autenticação recusada (HTTP 401): chave de API inválida ou ausente."
            )
        if exc.code == 503:
            raise _gateway_unavailable(exc)
        raise ClientError(
            f"O serviço Bonsai respondeu erro HTTP {exc.code}. Verifique o estado do serviço."
        )
    except (TimeoutError, socket.timeout):
        raise ClientError(f"Tempo limite de {timeout}s esgotado aguardando o serviço Bonsai.")
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            raise ClientError(
                f"Tempo limite de {timeout}s esgotado aguardando o serviço Bonsai."
            )
        raise ClientUnavailable(_indisponivel())
    except OSError:
        raise ClientUnavailable(_indisponivel())
    return parse_response(raw)


def _gateway_unavailable(error: urllib.error.HTTPError) -> ClientUnavailable:
    """Classifica somente códigos conhecidos sem expor o corpo HTTP."""
    try:
        raw = error.read(MAX_GATEWAY_ERROR_BODY_BYTES)
        if not isinstance(raw, bytes):
            return ClientUnavailable(_generic_gateway_unavailable())
        data = json.loads(raw.decode("utf-8"))
        gateway_error = data.get("error") if isinstance(data, dict) else None
        code = gateway_error.get("code") if isinstance(gateway_error, dict) else None
    except (OSError, UnicodeDecodeError, TypeError, ValueError):
        code = None
    return ClientUnavailable(GATEWAY_ERROR_MESSAGES.get(code, _generic_gateway_unavailable()))


def _generic_gateway_unavailable() -> str:
    return "BONSAI_UNAVAILABLE: gateway Bonsai indisponível; use o próximo modelo/provedor."


def _indisponivel() -> str:
    return (
        "BONSAI_UNAVAILABLE: serviço Bonsai indisponível em 127.0.0.1:10115. "
        "Use o próximo modelo/provedor ou inicie-o com `bonsai-server start`."
    )


def read_prompt(prompt: Optional[str], file_path: Optional[str], stdin=sys.stdin) -> str:
    """Resolve a origem do prompt: --file, argumento ou stdin (nessa ordem)."""
    if file_path is not None:
        if prompt is not None:
            raise ClientError(
                "Use apenas uma origem de prompt: --file OU argumento/stdin."
            )
        try:
            return Path(file_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise ClientError(f"Não foi possível ler o arquivo de prompt: {file_path}")
    if prompt is not None:
        return prompt
    if hasattr(stdin, "isatty") and stdin.isatty():
        raise ClientError(
            "Nenhum prompt recebido: passe um argumento, --file ARQUIVO ou "
            "envie texto pela stdin."
        )
    try:
        return stdin.read()
    except (OSError, UnicodeDecodeError):
        raise ClientError("Não foi possível ler a entrada padrão (stdin).")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bonsai-ask",
        description="Consulta o serviço local Bonsai (somente loopback) e imprime a resposta.",
        epilog=(
            "Sem prompt e sem --file, lê o texto da stdin. Não faz auto-start: "
            "inicie o serviço com `bonsai-server start`."
        ),
    )
    parser.add_argument(
        "prompt", nargs="?", default=None, help="Prompt textual (opcional)."
    )
    parser.add_argument(
        "--file",
        default=None,
        metavar="ARQUIVO",
        help="Lê o prompt deste arquivo (somente se indicado explicitamente).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Segundos de espera (1-{MAX_TIMEOUT}). Padrão: {DEFAULT_TIMEOUT}.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"Limite de tokens da resposta (1-{MAX_TOKENS_LIMIT}). Padrão: {DEFAULT_MAX_TOKENS}.",
    )
    return parser


def main(
    argv: Optional[list] = None,
    *,
    stdin=sys.stdin,
    key_path: Optional[Path] = None,
    urlopen: Optional[Urlopen] = None,
) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        validate_params(timeout=args.timeout, max_tokens=args.max_tokens)
        prompt = read_prompt(args.prompt, args.file, stdin=stdin)
        content = query(
            prompt,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
            key_path=key_path if key_path is not None else DEFAULT_KEY_PATH,
            urlopen=urlopen if urlopen is not None else _open_no_redirect,
        )
    except ClientError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE if isinstance(exc, ClientUnavailable) else 1
    except KeyboardInterrupt:
        print("interrompido", file=sys.stderr)
        return 130
    except Exception:
        # Defesa final da CLI: nenhum traceback, nenhum corpo/segredo na saída.
        print("erro inesperado ao consultar o serviço Bonsai.", file=sys.stderr)
        return 1
    print(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
