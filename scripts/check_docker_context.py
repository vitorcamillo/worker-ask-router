#!/usr/bin/env python3
"""Gate do contexto de build: o que o `COPY .` levaria nao pode ter sensivel.

O Dockerfile copia o repo inteiro filtrado so pelo .dockerignore; num build
local a arvore de trabalho tambem tem arquivos fora do git (api-key, *.key,
bancos, logs, sessions/, runtime/, .claude/, .worker-ask-isolation) que o
.gitignore segura do commit mas que entrariam na imagem. O check aplica as
regras do .dockerignore (subconjunto do docker: `*`, `?`, `**`, `!` e padrao
com barra final; vale o ultimo padrao que casa, default entrar) sobre a
arvore de --root (padrao: o repo do script), conta o contexto resultante e
sai 1 se algum caminho sensivel entrar, 0 se nenhum. `--listar` imprime o
contexto inteiro, um caminho por linha.

Somente stdlib; sem privilegios, contas ou rede; nada e gravado na arvore.
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (rotulo, regex sobre o caminho relativo posix) do que nunca entra na imagem.
SENSIVEIS = tuple(
    (rotulo, re.compile(padrao))
    for rotulo, padrao in (
        ("segredo", r"(^|/)(api-key|auth\.json|credentials\.json)$"),
        ("chave-privada", r"\.key$"),
        ("banco-de-dados", r"\.db|\.sqlite"),
        ("log", r"\.log$"),
        ("backup", r"\.bak"),
        ("env", r"(^|/)\.env"),
        ("marcador-de-isolamento", r"(^|/)\.worker-ask-isolation$"),
        ("diretorio-local", r"(^|/)(sessions|runtime|node_modules|\.venv|\.claude|\.ruff_cache)(/|$)"),
    )
)


def _regex(padrao):
    """Compila um padrao do .dockerignore.

    `**/` casa zero ou mais diretorios (`**/foo` pega `foo` na raiz, como no
    docker); `**` solto cruza diretorios; `*` e `?` ficam no segmento.
    """
    saida = []
    i = 0
    while i < len(padrao):
        if padrao.startswith("**/", i):
            saida.append("(?:.*/)?")
            i += 3
        elif padrao.startswith("**", i):
            saida.append(".*")
            i += 2
        elif padrao[i] == "*":
            saida.append("[^/]*")
            i += 1
        elif padrao[i] == "?":
            saida.append("[^/]")
            i += 1
        else:
            saida.append(re.escape(padrao[i]))
            i += 1
    return re.compile("".join(saida))


def carregar(texto):
    """Linhas do .dockerignore em (excluir, regex, so_diretorio), na ordem."""
    padroes = []
    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        excluir = True
        if linha.startswith("!"):
            excluir = False
            linha = linha[1:]
        so_diretorio = linha.endswith("/")
        linha = linha.rstrip("/") or linha
        if linha.startswith("/"):  # barra inicial e so ancora na raiz
            linha = linha[1:]
        padroes.append((excluir, _regex(linha), so_diretorio))
    return padroes


def excluido(caminho, padroes):
    """Decisao do docker para um caminho: vale o ultimo padrao que casar."""
    partes = caminho.split("/")
    fora = False
    for excluir, regex, so_diretorio in padroes:
        # O padrao casa o caminho inteiro ou algum diretorio ancestral.
        limite = len(partes) - 1 if so_diretorio else len(partes)
        for i in range(1, limite + 1):
            if regex.fullmatch("/".join(partes[:i])):
                fora = excluir
                break
    return fora


def contexto(root):
    """Caminhos relativos (posix) que o `COPY .` levaria da arvore em root."""
    padroes = carregar((root / ".dockerignore").read_text(encoding="utf-8"))
    for base, diretorios, arquivos in os.walk(root):
        diretorios.sort()
        for nome in sorted(arquivos):
            caminho = Path(base, nome).relative_to(root).as_posix()
            if not excluido(caminho, padroes):
                yield caminho


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root = ROOT
    listar = False
    i = 0
    while i < len(argv):
        if argv[i] == "--root" and i + 1 < len(argv):
            root = Path(argv[i + 1]).resolve()
            i += 2
            continue
        if argv[i] == "--listar":
            listar = True
            i += 1
            continue
        print(f"check_docker_context: opcao desconhecida ou incompleta: {argv[i]}",
              file=sys.stderr)
        return 2
    if not (root / ".dockerignore").is_file():
        print(f"check_docker_context: {root}/.dockerignore nao existe", file=sys.stderr)
        return 2
    ofensores = []
    total = 0
    for caminho in contexto(root):
        total += 1
        if listar:
            print(caminho)
        rotulo = next((r for r, busca in SENSIVEIS if busca.search(caminho)), None)
        if rotulo:
            ofensores.append((caminho, rotulo))
    if ofensores:
        print(f"check_docker_context: {len(ofensores)} caminho(s) sensivel(is) "
              "no contexto de build:", file=sys.stderr)
        for caminho, rotulo in ofensores:
            print(f"  {caminho} ({rotulo})", file=sys.stderr)
        print("  corrija o .dockerignore antes de construir a imagem", file=sys.stderr)
        return 1
    print(f"check_docker_context: contexto de build com {total} arquivos, "
          "nenhum sensivel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
