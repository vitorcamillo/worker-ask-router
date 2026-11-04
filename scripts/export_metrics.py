#!/usr/bin/env python3
"""Exporta metricas REAIS de cobertura (coverage.py) e mutacao (mutmut meta).

Dois subcomandos, ambos adaptadores de formato para o runner existente
(scripts/quality_gate.py), sem executar JaCoCo nem PIT:

  coverage  le artifacts/coverage.json (relatorio JSON do coverage.py), seleciona
            apenas o modulo de producao (harness_contract.py), e grava
            target/site/jacoco/jacoco.xml (counters LINE/BRANCH) e
            artifacts/coverage-metrics.json. Falha com fonte ausente/invalida,
            zero declaracoes, zero ramos ou contagens inconsistentes; nada e
            inventado.

  mutation  le mutants/harness_contract.py.meta (mutmut 3: exit_code_by_key;
            --source aceita outro caminho) e grava target/pit-reports/mutations.xml
            e artifacts/mutation-metrics.json. detected=true SOMENTE para mutante
            morto com exit 1. A tabela de status abaixo foi inspecionada pelo
            orquestrador no mutmut 3.2.2 local; exit code fora dela falha em vez
            de ser adivinhado. Exit 3/-24 (killed para o mutmut) viram tool_error
            e falham a exportacao: o gate nao pode passar por erro de pytest
            disfarçado de kill. Sobreviventes e timeouts permanecem sem detected;
            o limiar decide. Nenhum mutante 'not checked' e aceito.

Somente stdlib; sem shell; nao le nem emite credenciais ou payload de agentes;
mutmut nao e importado — o meta e lido como dado.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODULE = "harness_contract.py"
COVERAGE_JSON_REL = Path("artifacts") / "coverage.json"
JACOCO_REL = Path("target/site/jacoco/jacoco.xml")
COVERAGE_METRICS_REL = Path("artifacts") / "coverage-metrics.json"
MUTANTS_META_REL = Path("mutants") / "harness_contract.py.meta"
PIT_XML_REL = Path("target/pit-reports/mutations.xml")
MUTATION_METRICS_REL = Path("artifacts") / "mutation-metrics.json"

# Tabela observada pelo orquestrador em mutmut.__main__.status_by_exit_code
# (mutmut 3.2.2 instalado localmente). Nao adivinhar: codigo fora dela vira
# "unknown" e falha a exportacao.
STATUS_BY_EXIT_CODE = {
    1: "killed",
    3: "killed",
    -24: "killed",
    0: "survived",
    5: "no tests",
    33: "no tests",
    2: "interrupted",
    None: "not checked",
    34: "skipped",
    35: "suspicious",
    36: "timeout",
    24: "timeout",
    152: "timeout",
    255: "timeout",
}
KILLED_EXIT = 1
# mutmut conta 3/-24 como killed; aqui viram tool_error, entram no total sem
# detected e falham a exportacao (exit 1).
TOOL_ERROR_EXITS = (3, -24)
NOT_EXECUTED = ("not checked", "no tests", "skipped", "interrupted")
PIT_STATUS = {
    "killed": "KILLED",
    "survived": "SURVIVED",
    "timeout": "TIMED_OUT",
    "tool_error": "RUN_ERROR",
    "not_executed": "RUN_ERROR",
    "suspicious": "RUN_ERROR",
    "unknown": "RUN_ERROR",
}
ERROR_REASONS = {
    "tool_error": "exit 3/-24 (killed na tabela do mutmut) tratado como tool_error: kill sustentado por erro de ferramenta nao e kill",
    "not_executed": "mutantes nao executados (not checked / no tests / skipped / interrupted)",
    "suspicious": "mutantes suspicious (anomalia de execucao detectada pelo mutmut)",
    "unknown": "exit code fora da tabela inspecionada; sem classificacao confiavel",
}


class ExportError(Exception):
    """Fonte de dados invalida; nada deve ser inventado no lugar."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _reject_constant(name):
    raise ValueError(f"constante JSON nao permitida: {name}")


def _load_json_strict(path):
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)


def _write_text_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_json_atomic(path, payload):
    _write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


# ----------------------------------------------------------------- coverage

def _count(summary, field):
    value = summary.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExportError(f"summary.{field} deve ser inteiro >= 0 (sem bool/float)")
    return value


def _coverage_block(covered, total):
    return {
        "covered": covered,
        "missed": total - covered,
        "total": total,
        "percent": round(100.0 * covered / total, 2),
    }


def extract_coverage(data, module):
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ExportError("raiz do coverage.json deve ter mapa 'files'")
    entry = data["files"].get(module)
    if not isinstance(entry, dict) or not isinstance(entry.get("summary"), dict):
        raise ExportError(f"entrada/summary ausente para o modulo {module!r}")
    summary = entry["summary"]
    covered_lines = _count(summary, "covered_lines")
    num_statements = _count(summary, "num_statements")
    covered_branches = _count(summary, "covered_branches")
    num_branches = _count(summary, "num_branches")
    if num_statements == 0:
        raise ExportError("num_statements == 0: nenhuma declaracao medida")
    if num_branches == 0:
        raise ExportError("num_branches == 0: ramos nao medidos; exportacao de branch recusada")
    if covered_lines > num_statements:
        raise ExportError("covered_lines > num_statements: contagens inconsistentes")
    if covered_branches > num_branches:
        raise ExportError("covered_branches > num_branches: contagens inconsistentes")
    return {
        "lines": _coverage_block(covered_lines, num_statements),
        "branches": _coverage_block(covered_branches, num_branches),
    }


def build_jacoco_xml(module, lines, branches):
    blocks = (("LINE", lines), ("BRANCH", branches))
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">',
        f"<report name={quoteattr('harness-contracts')}>",
        f"  <package name={quoteattr('harness-contracts')}>",
        f"    <sourcefile name={quoteattr(module)}>",
    ]
    parts = parts[:3]  # counters precisam ser filhos diretos de report para o gate.
    for indent in ("  ",):
        for kind, block in blocks:
            parts.append(
                f"{indent}<counter type={quoteattr(kind)}"
                f" missed=\"{block['missed']}\" covered=\"{block['covered']}\"/>"
            )
    parts.extend(["</report>", ""])
    return "\n".join(parts)


def export_coverage(args):
    try:
        data = _load_json_strict(args.coverage_json)
        metrics = extract_coverage(data, args.module)
    except (OSError, UnicodeDecodeError, ValueError, ExportError) as exc:
        print(f"EXPORT coverage: FAIL fonte invalida ({args.coverage_json}): {exc}", file=sys.stderr)
        return 1
    jacoco_path = args.root / JACOCO_REL
    _write_text_atomic(
        jacoco_path, build_jacoco_xml(args.module, metrics["lines"], metrics["branches"])
    )
    report = {
        "schema_version": 1,
        "metric": "coverage",
        "generated_at": _now(),
        "source": {
            "file": str(args.coverage_json),
            "origin": "coverage.py (relatorio JSON)",
            "module": args.module,
            "selection": "somente o modulo de producao; demais arquivos do mapa ignorados",
        },
        "lines": metrics["lines"],
        "branches": metrics["branches"],
        "artifacts": {"jacoco_xml": str(JACOCO_REL)},
        "notes": [
            "adaptador de formato: counters LINE/BRANCH do jacoco.xml derivados do summary do coverage.py",
            "o runner quality_gate.py le os counters no nivel raiz do relatorio",
        ],
    }
    _write_json_atomic(args.root / COVERAGE_METRICS_REL, report)
    print(
        f"EXPORT coverage: OK module={args.module}"
        f" lines={metrics['lines']['percent']}% branches={metrics['branches']['percent']}% -> {JACOCO_REL}"
    )
    return 0


# ----------------------------------------------------------------- mutation

def load_mutant_exits(meta):
    if not isinstance(meta, dict) or not isinstance(meta.get("exit_code_by_key"), dict):
        raise ExportError("meta sem 'exit_code_by_key' (formato mutmut 3 esperado)")
    exits = meta["exit_code_by_key"]
    if not exits:
        raise ExportError("exit_code_by_key vazio: nenhum mutante registrado")
    parsed = {}
    for key, code in exits.items():
        if not isinstance(key, str) or not key:
            raise ExportError("chave de mutante invalida no meta")
        if code is None or (isinstance(code, int) and not isinstance(code, bool)):
            parsed[key] = code
        else:
            raise ExportError("exit code deve ser inteiro ou null")
    return parsed


def classify_exit_code(code):
    if code == KILLED_EXIT:
        return "killed"
    if code in TOOL_ERROR_EXITS:
        return "tool_error"
    status = STATUS_BY_EXIT_CODE.get(code, "unknown")
    if status == "survived":
        return "survived"
    if status == "timeout":
        return "timeout"
    if status in NOT_EXECUTED:
        return "not_executed"
    if status == "suspicious":
        return "suspicious"
    return "unknown"


def summarize_mutants(exits):
    per = []
    by_class = {}
    by_exit = {}
    for key in sorted(exits):
        code = exits[key]
        cls = classify_exit_code(code)
        per.append({"key": key, "exit_code": code, "class": cls})
        by_class[cls] = by_class.get(cls, 0) + 1
        label = str(code)
        by_exit[label] = by_exit.get(label, 0) + 1
    return per, by_class, by_exit


def build_mutations_xml(source_file, per):
    class_name = source_file[: -len(".py")] if source_file.endswith(".py") else source_file
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<mutations>"]
    for index, item in enumerate(per, start=1):
        mutmut_status = STATUS_BY_EXIT_CODE.get(item["exit_code"], "fora da tabela")
        detected = "true" if item["class"] == "killed" else "false"
        description = (
            f"{item['class']}; exit={item['exit_code']}; status_mutmut={mutmut_status}; "
            "adaptador do meta mutmut (linha nao disponivel)"
        )
        parts.append(
            f"  <mutation detected={quoteattr(detected)} status={quoteattr(PIT_STATUS[item['class']])}>"
            f"<sourceFile>{escape(source_file)}</sourceFile>"
            f"<mutatedClass>{escape(class_name)}</mutatedClass>"
            f"<mutatedMethod>{escape(item['key'])}</mutatedMethod>"
            f"<mutator>mutmut</mutator>"
            f"<description>{escape(description)}</description>"
            f"<index>{index}</index>"
            f"</mutation>"
        )
    parts.extend(["</mutations>", ""])
    return "\n".join(parts)


def export_mutation(args):
    try:
        meta = _load_json_strict(args.source)
        exits = load_mutant_exits(meta)
    except (OSError, UnicodeDecodeError, ValueError, ExportError) as exc:
        print(f"EXPORT mutation: FAIL fonte invalida ({args.source}): {exc}", file=sys.stderr)
        return 1
    per, by_class, by_exit = summarize_mutants(exits)
    total = len(per)
    killed = by_class.get("killed", 0)
    errors = [
        f"{by_class.get(cls, 0)} mutante(s) {cls}: {reason}"
        for cls, reason in ERROR_REASONS.items()
        if by_class.get(cls, 0)
    ]
    source_file = args.source.name
    if source_file.endswith(".meta"):
        source_file = source_file[: -len(".meta")]
    _write_text_atomic(args.root / PIT_XML_REL, build_mutations_xml(source_file, per))
    ordered_codes = sorted(
        STATUS_BY_EXIT_CODE, key=lambda code: (code is None, code if code is not None else 0)
    )
    report = {
        "schema_version": 1,
        "metric": "mutation",
        "generated_at": _now(),
        "source": {
            "file": str(args.source),
            "origin": "mutmut meta (exit_code_by_key); mutmut nao importado, meta lido como dado",
            "source_file": source_file,
        },
        "status_table": {
            "source": "mutmut.__main__.status_by_exit_code inspecionada pelo orquestrador no mutmut 3.2.2 local",
            "exit_code_to_status": {str(code): STATUS_BY_EXIT_CODE[code] for code in ordered_codes},
        },
        "counts": {"total": total, "by_class": by_class, "by_exit_code": by_exit},
        "score_pct": round(100.0 * killed / total, 2),
        "detected_policy": (
            "detected=true somente para exit 1; survived e timeout permanecem sem detected e o limiar decide"
        ),
        "mutmut_counts_as_killed": {
            "exit_codes": list(TOOL_ERROR_EXITS),
            "count": by_class.get("tool_error", 0),
            "note": (
                "mutmut 3.2.2 classifica 3/-24 como killed; este exportador marca tool_error "
                "e falha com exit 1 — gate nao pode passar por erro disfarçado"
            ),
        },
        "errors": errors,
        "artifacts": {"pit_xml": str(PIT_XML_REL)},
        "notes": [
            "adaptador de formato para o runner quality_gate.py (mutations.xml estilo PIT); PIT nao foi executado",
        ],
    }
    _write_json_atomic(args.root / MUTATION_METRICS_REL, report)
    if errors:
        for message in errors:
            print(f"EXPORT mutation: FAIL {message}", file=sys.stderr)
        return 1
    print(f"EXPORT mutation: OK killed={killed}/{total} score={report['score_pct']}% -> {PIT_XML_REL}")
    return 0


# --------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Exporta metricas reais (coverage.py/mutmut meta) para o runner do quality gate"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    coverage = subparsers.add_parser(
        "coverage", help="converte artifacts/coverage.json em jacoco.xml + coverage-metrics.json"
    )
    coverage.add_argument("--root", type=Path, default=ROOT)
    coverage.add_argument("--coverage-json", type=Path, default=None,
                          help="default: <root>/artifacts/coverage.json")
    coverage.add_argument("--module", default=DEFAULT_MODULE,
                          help="modulo de producao selecionado no relatorio")

    mutation = subparsers.add_parser(
        "mutation", help="converte mutants/<modulo>.meta em mutations.xml + mutation-metrics.json"
    )
    mutation.add_argument("--root", type=Path, default=ROOT)
    mutation.add_argument("--source", type=Path, default=None,
                          help="default: <root>/mutants/harness_contract.py.meta")

    args = parser.parse_args(argv)
    if args.command == "coverage":
        if args.coverage_json is None:
            args.coverage_json = args.root / COVERAGE_JSON_REL
        return export_coverage(args)
    if args.source is None:
        args.source = args.root / MUTANTS_META_REL
    return export_mutation(args)


if __name__ == "__main__":
    sys.exit(main())
