#!/usr/bin/env python3
"""Planeja, executa e valida o quality gate universal dos agentes.

O plano usa argv (nunca shell=True) para que o agente possa registrar comandos
reproduzíveis sem transformar saída do modelo em código executável.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CATEGORIAS = (
    "unit",
    "gherkin",
    "quality",
    "metrics",
    "mutation",
    "coverage",
    "acceptance",
    "qa",
    "e2e",
)
STATUS = {"NOT_RUN", "PASS", "FAIL", "BLOCKED", "N/A"}
METRICAS = (
    "unit_pass_rate",
    "gherkin_pass_rate",
    "acceptance_pass_rate",
    "qa_pass_rate",
    "e2e_pass_rate",
    "coverage_line_pct",
    "coverage_branch_pct",
    "mutation_score_pct",
)
METRICAS_CRITICAS = (
    "coverage_line_pct",
    "coverage_branch_pct",
    "mutation_score_pct",
)
_TAXAS = {
    "unit": "unit_pass_rate",
    "gherkin": "gherkin_pass_rate",
    "acceptance": "acceptance_pass_rate",
    "qa": "qa_pass_rate",
    "e2e": "e2e_pass_rate",
}
_SEGREDO = re.compile(r"(?i)(token|password|passwd|secret|api[_-]?key|cookie)\s*[:=]\s*([^\s,;]+)")


def _numero_mensuravel(valor: Any) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor)


def _avaliar_thresholds(
    check: dict[str, Any], metricas: dict[str, Any]
) -> list[tuple[str, str]]:
    """Devolve (tipo, motivo) para thresholds inválidos ou não atendidos."""

    if "thresholds" not in check:
        return []
    thresholds = check.get("thresholds")
    if not isinstance(thresholds, dict) or not thresholds:
        return [("invalid", "thresholds deve ser um objeto não vazio")]

    problemas: list[tuple[str, str]] = []
    for nome, minimo in thresholds.items():
        if nome not in METRICAS:
            problemas.append(("invalid", f"threshold desconhecido ({nome})"))
            continue
        if not _numero_mensuravel(minimo) or minimo < 0:
            problemas.append(("invalid", f"threshold inválido para {nome}"))
            continue
        valor = metricas.get(nome)
        if not _numero_mensuravel(valor):
            problemas.append(("missing", f"threshold exige métrica numérica ({nome})"))
        elif valor < minimo:
            problemas.append(("below", f"{nome} abaixo do threshold ({valor} < {minimo})"))
    return problemas


def _problemas_threshold_categoria(
    categoria: str, check: dict[str, Any], metricas: dict[str, Any]
) -> list[tuple[str, str]]:
    if categoria in {"coverage", "mutation"} and "thresholds" not in check:
        return [("invalid", "thresholds explícitos são obrigatórios")]
    return _avaliar_thresholds(check, metricas)


def _percentual(cobertos: int, perdidos: int) -> float | None:
    total = cobertos + perdidos
    return None if total <= 0 else round(100 * cobertos / total, 2)


def _coletar_metricas_relatorios(raiz: Path) -> dict[str, float]:
    """Extrai métricas dos artefatos produzidos pelos comandos JaCoCo e PIT."""

    metricas: dict[str, float] = {}
    contadores = {"LINE": [0, 0], "BRANCH": [0, 0]}
    for caminho in raiz.glob("*/target/site/jacoco/jacoco.xml"):
        try:
            relatorio = ET.parse(caminho).getroot()
        except (ET.ParseError, OSError):
            continue
        for contador in relatorio.findall("counter"):
            tipo = contador.get("type")
            if tipo not in contadores:
                continue
            contadores[tipo][0] += int(contador.get("covered", "0"))
            contadores[tipo][1] += int(contador.get("missed", "0"))
    linhas = _percentual(*contadores["LINE"])
    branches = _percentual(*contadores["BRANCH"])
    if linhas is not None:
        metricas["coverage_line_pct"] = linhas
    if branches is not None:
        metricas["coverage_branch_pct"] = branches

    total_mutacoes = 0
    mutacoes_detectadas = 0
    for caminho in raiz.glob("*/target/pit-reports/mutations.xml"):
        try:
            relatorio = ET.parse(caminho).getroot()
        except (ET.ParseError, OSError):
            continue
        for mutacao in relatorio.findall("mutation"):
            total_mutacoes += 1
            detectada = str(mutacao.get("detected", "")).lower() == "true"
            if detectada or mutacao.get("status") == "KILLED":
                mutacoes_detectadas += 1
    mutacao = _percentual(mutacoes_detectadas, total_mutacoes - mutacoes_detectadas)
    if mutacao is not None:
        metricas["mutation_score_pct"] = mutacao
    return metricas


def _coletar_metricas_categoria(raiz: Path, categoria: str) -> dict[str, float]:
    """Evita que artefato residual de outra ferramenta sobrescreva a prova do bloco."""

    todas = _coletar_metricas_relatorios(raiz)
    permitidas = {
        "coverage": {"coverage_line_pct", "coverage_branch_pct"},
        "mutation": {"mutation_score_pct"},
    }.get(categoria, set())
    return {nome: valor for nome, valor in todas.items() if nome in permitidas}


def _limpar_relatorios_criticos(raiz: Path, categoria: str) -> None:
    padrao = {
        "coverage": "*/target/site/jacoco/jacoco.xml",
        "mutation": "*/target/pit-reports/mutations.xml",
    }.get(categoria)
    if padrao is None:
        return
    for caminho in raiz.glob(padrao):
        try:
            caminho.unlink()
        except FileNotFoundError:
            pass


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redigir(texto: str, limite: int = 2000) -> str:
    texto = _SEGREDO.sub(r"\1=[REDACTED]", texto)
    texto = re.sub(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_\-]{12,}\b", "[REDACTED]", texto)
    return texto[-limite:]


def _evidencia_existe(relatorio: dict[str, Any], item: Any) -> bool:
    if not isinstance(item, str) or not item.strip():
        return False
    if item in {"command_results", "stdout", "stderr"}:
        return True
    caminho = item.split("#", 1)[0]
    if caminho.startswith(("http://", "https://")):
        return True
    raiz = Path(str(relatorio.get("project_root") or ".")).resolve()
    alvo = Path(caminho)
    if not alvo.is_absolute():
        alvo = raiz / alvo
    return alvo.exists()


def _check() -> dict[str, Any]:
    return {
        "applicable": True,
        "status": "NOT_RUN",
        "commands": [],
        "command_results": [],
        "evidence": [],
        "reason": "",
    }


def novo_plano(root: str | Path) -> dict[str, Any]:
    """Cria um plano que não permite esquecer nenhuma categoria."""

    return {
        "schema_version": 1,
        "project_root": str(Path(root).resolve()),
        "created_at": _agora(),
        "scope": {"changed_files": [], "risk": "não classificado"},
        "checks": {categoria: _check() for categoria in CATEGORIAS},
        "metrics": {nome: None for nome in METRICAS},
    }


def validar_relatorio(relatorio: dict[str, Any]) -> list[str]:
    """Retorna todos os motivos que impedem a liberação."""

    erros: list[str] = []
    if not isinstance(relatorio, dict) or relatorio.get("schema_version") != 1:
        return ["schema_version deve ser 1"]

    checks = relatorio.get("checks")
    if not isinstance(checks, dict):
        return ["checks deve ser um objeto"]

    faltantes = [categoria for categoria in CATEGORIAS if categoria not in checks]
    if faltantes:
        erros.append(f"categorias ausentes: {', '.join(faltantes)}")

    for categoria in CATEGORIAS:
        check = checks.get(categoria)
        if not isinstance(check, dict):
            erros.append(f"{categoria}: check inválido")
            continue

        status = check.get("status")
        aplicavel = check.get("applicable", True)
        evidencia = check.get("evidence") or []
        motivo = str(check.get("reason") or "").strip()
        if status not in STATUS:
            erros.append(f"{categoria}: status inválido ({status!r})")
            continue
        if status == "NOT_RUN":
            erros.append(f"{categoria}: NOT_RUN não libera a entrega")
        if status == "N/A":
            if aplicavel is not False:
                erros.append(f"{categoria}: N/A exige applicable=false")
            if not motivo:
                erros.append(f"{categoria}: N/A exige reason")
            if not evidencia:
                erros.append(f"{categoria}: N/A exige evidence")
        elif aplicavel is False:
            erros.append(f"{categoria}: applicable=false exige status N/A")
        if status == "PASS" and not (evidencia or check.get("command_results")):
            erros.append(f"{categoria}: PASS exige commands ou evidence")
        if status in {"FAIL", "BLOCKED"}:
            erros.append(f"{categoria}: {status} ({motivo or 'sem motivo'})")
        if status in {"PASS", "N/A"}:
            invalidas = [item for item in evidencia if not _evidencia_existe(relatorio, item)]
            if invalidas:
                erros.append(
                    f"{categoria}: evidence inexistente ({', '.join(map(str, invalidas))})"
                )

    metricas = relatorio.get("metrics")
    if not isinstance(metricas, dict):
        erros.append("metrics deve ser um objeto com valores mensuráveis")
    else:
        for nome in METRICAS:
            if nome not in metricas:
                erros.append(f"metrics: métrica ausente ({nome})")
        por_categoria = {
            "unit": "unit_pass_rate",
            "gherkin": "gherkin_pass_rate",
            "acceptance": "acceptance_pass_rate",
            "qa": "qa_pass_rate",
            "e2e": "e2e_pass_rate",
            "coverage": "coverage_line_pct",
            "mutation": "mutation_score_pct",
        }
        for categoria, nome in por_categoria.items():
            check = checks.get(categoria, {})
            if check.get("status") == "PASS":
                valor = metricas.get(nome)
                if not _numero_mensuravel(valor):
                    erros.append(f"metrics: {nome} deve ser numérica quando {categoria}=PASS")
                elif valor <= 0:
                    erros.append(f"metrics: {nome} não pode ser zero quando {categoria}=PASS")
        if checks.get("coverage", {}).get("status") == "PASS":
            valor = metricas.get("coverage_branch_pct")
            if not _numero_mensuravel(valor):
                erros.append("metrics: coverage_branch_pct deve ser numérica quando coverage=PASS")
            elif valor <= 0:
                erros.append("metrics: coverage_branch_pct não pode ser zero quando coverage=PASS")
        if checks.get("metrics", {}).get("status") == "PASS" and not any(
            _numero_mensuravel(metricas.get(nome)) for nome in METRICAS
        ):
            erros.append("metrics: PASS exige ao menos uma métrica numérica")
        if checks.get("metrics", {}).get("status") == "PASS":
            for nome in METRICAS_CRITICAS:
                valor = metricas.get(nome)
                if not _numero_mensuravel(valor):
                    erros.append(f"metrics: {nome} deve ser numérica quando metrics=PASS")
                elif valor <= 0:
                    erros.append(f"metrics: {nome} não pode ser zero quando metrics=PASS")
        for categoria in CATEGORIAS:
            check = checks.get(categoria, {})
            if check.get("status") == "PASS":
                for _, motivo in _problemas_threshold_categoria(categoria, check, metricas):
                    erros.append(f"{categoria}: {motivo}")

    return erros


def _argv(command: Any) -> list[str] | None:
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) for item in command)
    ):
        return None
    return command


def _executar_comando(command: list[str], root: Path, timeout: float) -> dict[str, Any]:
    inicio = time.monotonic()
    base: dict[str, Any] = {"argv": command}
    try:
        processo = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        base.update(returncode=None, error=f"comando não encontrado: {exc}")
    except subprocess.TimeoutExpired as exc:
        base.update(
            returncode=None,
            error=f"timeout após {timeout:.1f}s",
            stdout=_redigir(str(exc.stdout or "")),
            stderr=_redigir(str(exc.stderr or "")),
        )
    else:
        base.update(
            returncode=processo.returncode,
            stdout=_redigir(processo.stdout),
            stderr=_redigir(processo.stderr),
        )
    base["duration_s"] = round(time.monotonic() - inicio, 3)
    return base


def executar_plano(
    plano: dict[str, Any], root: str | Path | None = None, timeout: float = 900.0
) -> dict[str, Any]:
    """Executa os comandos declarados e devolve um relatório verificável."""

    relatorio = copy.deepcopy(plano)
    raiz = Path(root or plano.get("project_root") or ".").resolve()
    for categoria in CATEGORIAS:
        check = relatorio["checks"].get(categoria, {})
        if check.get("applicable") is False:
            check["status"] = "N/A"
            continue
        comandos = check.get("commands") or []
        _limpar_relatorios_criticos(raiz, categoria)
        resultados: list[dict[str, Any]] = []
        comandos_invalidos = False
        for comando in comandos:
            argv = _argv(comando)
            if argv is None:
                comandos_invalidos = True
                resultados.append(
                    {"argv": comando, "returncode": None, "error": "command deve ser argv[]"}
                )
                continue
            resultados.append(_executar_comando(argv, raiz, timeout))
        check["command_results"] = resultados
        if not comandos:
            check["status"] = "BLOCKED"
            check["reason"] = "nenhum comando foi declarado"
        elif comandos_invalidos or any(item.get("returncode") != 0 for item in resultados):
            check["status"] = "FAIL"
            check["reason"] = "um ou mais comandos falharam; consulte command_results"
        else:
            metric_name = _TAXAS.get(categoria)
            if metric_name:
                aprovados = sum(item.get("returncode") == 0 for item in resultados)
                relatorio.setdefault("metrics", {})[metric_name] = round(
                    100 * aprovados / len(resultados), 2
                )
            if categoria in {"coverage", "mutation"}:
                relatorio.setdefault("metrics", {}).update(
                    _coletar_metricas_categoria(raiz, categoria)
                )
            problemas = _problemas_threshold_categoria(
                categoria, check, relatorio.get("metrics", {})
            )
            if problemas:
                tipos = {tipo for tipo, _ in problemas}
                check["status"] = "BLOCKED" if tipos & {"invalid", "missing"} else "FAIL"
                check["reason"] = "; ".join(motivo for _, motivo in problemas)
            else:
                check["status"] = "PASS"
                check["evidence"] = list(
                    dict.fromkeys([*check.get("evidence", []), "command_results"])
                )
    # Artefatos críticos podem ser limpos por uma categoria posterior (por exemplo,
    # `clean test` da cobertura remove os XML do PIT). A métrica medida no próprio bloco é
    # a evidência autoritativa; a coleta final serve apenas para preencher o que ainda falta.
    finais = _coletar_metricas_relatorios(raiz)
    metricas = relatorio.setdefault("metrics", {})
    for nome, valor in finais.items():
        if not _numero_mensuravel(metricas.get(nome)):
            metricas[nome] = valor
    relatorio["finished_at"] = _agora()
    return relatorio


def _ler_json(caminho: Path) -> dict[str, Any]:
    valor = json.loads(caminho.read_text(encoding="utf-8"))
    if not isinstance(valor, dict):
        raise ValueError("o JSON raiz deve ser um objeto")
    return valor


def _escrever_json(caminho: Path, valor: dict[str, Any]) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(valor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quality gate universal para agentes")
    subparsers = parser.add_subparsers(dest="acao", required=True)

    prepare = subparsers.add_parser("prepare", help="cria plano com todas as categorias")
    prepare.add_argument("--root", default=".")
    prepare.add_argument("--output", default="quality-gate.json")

    run = subparsers.add_parser("run", help="executa os argv declarados no plano")
    run.add_argument("--plan", default="quality-gate.json")
    run.add_argument("--root", default=None)
    run.add_argument("--output", default="quality-gate-result.json")
    run.add_argument("--timeout", type=float, default=900.0)

    verify = subparsers.add_parser("verify", help="bloqueia relatório incompleto ou com falha")
    verify.add_argument("--report", default="quality-gate-result.json")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.acao == "prepare":
            _escrever_json(Path(args.output), novo_plano(args.root))
            print(f"QUALITY_GATE_PLAN: {args.output}")
            return 0
        if args.acao == "run":
            relatorio = executar_plano(_ler_json(Path(args.plan)), args.root, args.timeout)
            _escrever_json(Path(args.output), relatorio)
            erros = validar_relatorio(relatorio)
            print(f"QUALITY_GATE_REPORT: {args.output}")
            if erros:
                for erro in erros:
                    print(f"BLOCK: {erro}", file=sys.stderr)
                return 1
            return 0
        erros = validar_relatorio(_ler_json(Path(args.report)))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"QUALITY_GATE_ERROR: {exc}", file=sys.stderr)
        return 2
    if erros:
        for erro in erros:
            print(f"BLOCK: {erro}", file=sys.stderr)
        return 1
    print("QUALITY_GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
