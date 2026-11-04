#!/usr/bin/env python3
"""Gate de timeout/cache: mutacao e cobertura do delta de cache atual.

Modos:
  mutation  roda tests/test_quota_reset.py em copia temporaria do repo para
            4 mutantes unicos do delta atual (precedencia de cota sobre
            timeout; preservacao do marcador timeout numerico;
            TIMEOUT_COOLDOWN_SECONDS zerado; descarte de legado timeout).
            Baseline verde e mutante morto, em todos. Falha de
            infraestrutura (baseline vermelha, erro de coleta, timeout de
            ferramenta) nao conta como morte: bloqueia com saida 2.
  coverage  mede linhas e branches via coverage.py restrito ao escopo do
            delta (TIMEOUT_COOLDOWN_SECONDS, _cache_entry, guardas de
            save_cache e gravacao de timeout), usando
            tests/test_quota_reset.py e rcfile derivado de coverage.ini
            com patch=subprocess. Declara o escopo medido; nao alega
            cobertura fora dele.

Somente stdlib; sem privilegios, contas ou rede; comandos sempre argv,
nunca shell=True.
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_REL = "worker_router.py"
TEST = "tests/test_quota_reset.py"
OUT = ROOT / "out-timeout-cache"
PYTEST_TIMEOUT = 60
COVERAGE_PYTEST_TIMEOUT = 600

MUTATIONS = (
    (
        "quota-precedence-over-timeout",
        '                if previous and previous.get("blocked_reason") != "timeout" '
        'and entry.get("blocked_reason") == "timeout":',
        "                if False:  # mutante: timeout sobrescreve cota",
        "timeout_does_not_override",
    ),
    (
        "timeout-marker-preserved",
        '        if value.get("blocked_reason") == "timeout":\n'
        '            entry["blocked_reason"] = "timeout"',
        "        if False:  # mutante: descarta marcador timeout\n"
        '            entry["blocked_reason"] = "timeout"',
        "timeout_opens_circuit or timeout_circuit_expires",
    ),
    (
        "timeout-cooldown-nonzero",
        "TIMEOUT_COOLDOWN_SECONDS = 300.0",
        "TIMEOUT_COOLDOWN_SECONDS = 0.0",
        "timeout_opens_circuit_for_next_call",
    ),
    (
        "legacy-timeout-discarded",
        '        if value.get("blocked_reason") == "timeout":\n'
        "            return None",
        "        if False:  # mutante: preserva legado timeout\n"
        "            return None",
        "legacy_timeout",
    ),
)

SCOPE_FUNCTIONS = ("_cache_entry", "save_cache")
SCOPE_CONSTANT = "TIMEOUT_COOLDOWN_SECONDS"
LINE_MIN_DEFAULT = 85.0
BRANCH_MIN_DEFAULT = 75.0


def _base_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


def _tracked_files():
    try:
        raw = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"BLOCK: git ls-files indisponivel para copiar o repo ({exc})")
        return None
    return [item for item in raw.decode().split("\0") if item]


def _clean_pycache(path):
    shutil.rmtree(path.parent / "__pycache__", ignore_errors=True)


def _pytest(stage, selection):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", TEST, "-k", selection],
        cwd=stage,
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=PYTEST_TIMEOUT,
    )


def mutation():
    files = _tracked_files()
    if files is None:
        return 2
    source = (ROOT / SOURCE_REL).read_text()
    for name, old, _new, _selection in MUTATIONS:
        if source.count(old) != 1:
            print(f"BLOCK: mutante {name}: alvo nao e unico no fonte atual")
            return 2
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix="timeout-cache-mutation-") as directory:
            stage = Path(directory)
            for relative in files:
                origin = ROOT / relative
                if not origin.is_file():
                    continue
                dest = stage / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, dest)
            target = stage / SOURCE_REL
            for name, old, new, selection in MUTATIONS:
                try:
                    target.write_text(source)
                    _clean_pycache(target)
                    baseline = _pytest(stage, selection)
                    if baseline.returncode != 0:
                        print(f"BLOCK: baseline falhou para {name}; nao conta como morte")
                        print(baseline.stdout[-2000:])
                        print(baseline.stderr[-2000:])
                        return 2
                    target.write_text(source.replace(old, new, 1))
                    _clean_pycache(target)
                    changed = _pytest(stage, selection)
                except subprocess.TimeoutExpired:
                    print(f"BLOCK: timeout de ferramenta no mutante {name}")
                    return 2
                killed = (
                    changed.returncode == 1
                    and "FAILED" in changed.stdout
                    and "ERROR collecting" not in changed.stdout
                )
                results.append({"name": name, "selection": selection, "killed": killed})
                print(f"{name} {'killed' if killed else 'survived'}", flush=True)
                target.write_text(source)
    except OSError as exc:
        print(f"BLOCK: falha de I/O na copia temporaria ({exc})")
        return 2
    report = {
        "scope": "four targeted timeout/cache mutants of the current delta; not exhaustive",
        "source": SOURCE_REL,
        "test": TEST,
        "results": results,
        "mutation_score_pct": 100 * sum(x["killed"] for x in results) / len(results),
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "mutation.json").write_text(json.dumps(report, indent=2) + "\n")
    xml = ET.Element("mutations")
    for item in results:
        ET.SubElement(
            xml,
            "mutation",
            name=item["name"],
            detected=str(item["killed"]).lower(),
            status="KILLED" if item["killed"] else "SURVIVED",
        )
    path = OUT / "target/pit-reports/mutations.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(xml).write(path, encoding="unicode")
    print(json.dumps({"mutation_score_pct": report["mutation_score_pct"]}))
    return 0 if all(x["killed"] for x in results) else 1


def _scope_ranges():
    """Intervalos de linhas do delta: funcoes do cache + constante."""
    tree = ast.parse((ROOT / SOURCE_REL).read_text())
    ranges = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in SCOPE_FUNCTIONS:
            ranges.append((f"{SOURCE_REL}:{node.name}", node.lineno, node.end_lineno))
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if SCOPE_CONSTANT in targets:
                ranges.append((f"{SOURCE_REL}:{SCOPE_CONSTANT}", node.lineno, node.end_lineno))
    return ranges


def _derived_rcfile():
    """Rcfile derivado de coverage.ini, com patch=subprocess preservado."""
    base = (ROOT / "coverage.ini").read_text()
    if "patch" not in base or "subprocess" not in base:
        print("BLOCK: coverage.ini sem patch=subprocess; escopo de subprocesso nao garantido")
        return None
    OUT.mkdir(exist_ok=True)
    rcfile = OUT / "coverage-timeout.rc"
    rcfile.write_text(
        "[run]\n"
        "branch = True\n"
        "patch = subprocess\n"
        "parallel = True\n"
        "sigterm = True\n"
        f"include = */{SOURCE_REL}\n"
        f"data_file = {OUT}/.coverage-timeout\n"
    )
    return rcfile


def _coverage_step(argv, env, timeout):
    try:
        return subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"BLOCK: falha de ferramenta em {' '.join(argv[3:5])} ({exc})")
        return None


def coverage(line_min, branch_min):
    rcfile = _derived_rcfile()
    if rcfile is None:
        return 2
    env = _base_env()
    env["COVERAGE_RCFILE"] = str(rcfile)
    steps = [
        ([sys.executable, "-m", "coverage", "erase"], PYTEST_TIMEOUT),
        ([sys.executable, "-m", "coverage", "run", "--branch", "-m", "pytest", "-q", TEST, "tests/test_worker_router.py"], COVERAGE_PYTEST_TIMEOUT),
        ([sys.executable, "-m", "coverage", "combine"], PYTEST_TIMEOUT),
        ([sys.executable, "-m", "coverage", "json", "-o", str(OUT / "coverage.json")], PYTEST_TIMEOUT),
    ]
    for argv, timeout in steps:
        result = _coverage_step(argv, env, timeout)
        if result is None:
            return 2
        if result.returncode != 0:
            print(f"BLOCK: {' '.join(argv[3:5])} retornou {result.returncode}")
            print(result.stdout[-2000:])
            print(result.stderr[-2000:])
            return 1
    ranges = _scope_ranges()
    if not ranges:
        print("BLOCK: escopo do delta nao localizado no fonte atual")
        return 2
    try:
        data = json.loads((OUT / "coverage.json").read_text())
        key = next(k for k in data["files"] if k.endswith(SOURCE_REL))
        entry = data["files"][key]
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        print(f"BLOCK: coverage.json sem o modulo do escopo ({exc})")
        return 2
    def in_scope(n):
        return any(start <= n <= end for _, start, end in ranges)
    try:
        executed = {n for n in entry["executed_lines"] if in_scope(n)}
        missing = {n for n in entry["missing_lines"] if in_scope(n)}
        good = [v for v in entry["executed_branches"] if in_scope(v[0])]
        bad = [v for v in entry["missing_branches"] if in_scope(v[0])]
    except (KeyError, TypeError) as exc:
        print(f"BLOCK: formato inesperado em coverage.json ({exc})")
        return 2
    total_lines = len(executed | missing)
    total_branches = len(good) + len(bad)
    if total_lines <= 0 or total_branches <= 0:
        print("BLOCK: escopo sem linhas ou ramos mensuraveis")
        return 2
    metrics = {
        "coverage_line_pct": round(100 * len(executed) / total_lines, 2),
        "coverage_branch_pct": round(100 * len(good) / total_branches, 2),
    }
    scope = {
        "note": "measures only the current timeout/cache delta; not the whole file",
        "ranges": [{"scope": name, "first_line": start, "last_line": end} for name, start, end in ranges],
        "test": TEST,
        "rcfile": "derived from coverage.ini with patch=subprocess",
        "missing_lines": sorted(missing),
        "missing_branches": bad,
        "metrics": metrics,
    }
    (OUT / "coverage-scope.json").write_text(json.dumps(scope, indent=2) + "\n")
    xml = ET.Element("report")
    ET.SubElement(xml, "counter", type="LINE", covered=str(len(executed)), missed=str(len(missing)))
    ET.SubElement(xml, "counter", type="BRANCH", covered=str(len(good)), missed=str(len(bad)))
    path = OUT / "target/site/jacoco/jacoco.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(xml).write(path, encoding="unicode")
    print(json.dumps(metrics))
    if metrics["coverage_line_pct"] < line_min or metrics["coverage_branch_pct"] < branch_min:
        print(f"BLOCK: cobertura do escopo abaixo do minimo ({line_min}/{branch_min})")
        return 1
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] not in ("mutation", "coverage"):
        print("uso: check_timeout_cache.py mutation|coverage [--line-min X --branch-min Y]")
        return 2
    if argv[1] == "mutation":
        if len(argv) != 2:
            print("uso: check_timeout_cache.py mutation")
            return 2
        return mutation()
    line_min, branch_min = LINE_MIN_DEFAULT, BRANCH_MIN_DEFAULT
    rest = argv[2:]
    try:
        if "--line-min" in rest:
            line_min = float(rest[rest.index("--line-min") + 1])
        if "--branch-min" in rest:
            branch_min = float(rest[rest.index("--branch-min") + 1])
    except (ValueError, IndexError):
        print("uso: check_timeout_cache.py coverage [--line-min X --branch-min Y]")
        return 2
    return coverage(line_min, branch_min)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
