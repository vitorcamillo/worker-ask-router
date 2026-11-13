#!/usr/bin/env python3
"""Gate de isolamento: mutacao e cobertura de worker_isolation.py e integracao no roteador.

Modos:
  mutation  roda os testes de isolamento em copia temporaria do repo para os
            37 mutantes do delta (guardas do slot, overlay, devolucao, gc,
            prompt e fallback por estado). Restaura os dois fontes originais
            na copia, aplica uma troca por mutante e roda pytest com timeout
            por mutante. Baseline verde exigida. Morto = pytest rc 1 com
            FAILED e sem erro de coleta. Falha de infraestrutura (baseline
            vermelha, alvo nao unico, erro de coleta, timeout de ferramenta)
            nao conta como morte: bloqueia com saida 2. Nota minima: 80%.
  coverage  mede linhas e ramos do arquivo worker_isolation.py inteiro via
            coverage.py, rodando os testes de isolamento existentes no proprio
            repo com rcfile derivado gravado em out-isolamento/. Minimos
            padrao: linhas 90, ramos 85.

Somente stdlib; sem privilegios, contas ou rede; comandos sempre argv,
nunca shell=True; a copia temporaria nunca toca os fontes reais.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISO_REL = "worker_isolation.py"
ROUTER_REL = "worker_router.py"
SOURCES = (ISO_REL, ROUTER_REL)
OUT = ROOT / "out-isolamento"
PYTEST_TIMEOUT = 300
BASELINE_TIMEOUT = 600
COVERAGE_PYTEST_TIMEOUT = 600
MUTATION_MIN_DEFAULT = 80.0

# Abreviacoes da lista de mutantes.
U1 = "tests/test_isolation.py"
U2 = "tests/test_isolation_sync.py"
U3 = "tests/test_isolation_branches.py"
E = "tests/test_isolation_e2e.py"
F = "tests/test_state_fallback.py"
COVERAGE_TESTS = (U1, U2, U3)
LINE_MIN_DEFAULT = 90.0
BRANCH_MIN_DEFAULT = 85.0

# (nome, fonte, antes, depois, arquivos de teste). Cada "antes" precisa aparecer
# exatamente uma vez no fonte correspondente; o script confere antes de aplicar.
# Sobreviventes esperados (barreira dupla, mutante equivalente): "rejeitado-escapa"
# (o caminho com ".." tambem e recusado por _ancestors_not_symlink) e "gc-symlink"
# (a conferencia de realpath e o proprio rmtree tambem recusam symlink).
MUTATIONS = (
    ("guarda-area", ISO_REL,
     "if not slot_real.startswith(worktrees):",
     "if False:", (U1, U3)),
    ("guarda-toplevel", ISO_REL,
     "if os.path.realpath(os.fsdecode(proc.stdout).strip()) != slot_real:",
     "if False:", (U1, U3)),
    ("guarda-common-dir", ISO_REL,
     "if os.path.realpath(raw) != os.path.realpath(os.fspath(common_dir)):",
     "if False:", (U1, U3)),
    ("sem-hooks", ISO_REL,
     'NO_HOOKS = ("-c", "core.hooksPath=/dev/null")',
     "NO_HOOKS = ()", (U1,)),
    ("run-id-valido", ISO_REL,
     "if not _RUN_ID_RE.match(str(run_id)):",
     "if False:", (U1, U3)),
    ("overlay-sensivel", ISO_REL,
     "if any(deps.is_sensitive(p) for p in parts):",
     "if False:", (U1,)),
    ("overlay-aninhado", ISO_REL,
     'if rel.endswith("/"):',
     "if False:", (U1,)),
    ("conferencia-preparo", ISO_REL,
     "if snap_slot_before[rel][:2] != snap_before[rel][:2]:",
     "if False:", (U1, U3)),
    ("devolucao-conflito", ISO_REL,
     "if agora_real[:2] != prep.snap_before[rel][:2]:",
     "if False:", (U2,)),
    ("devolucao-symlink-ancestral", ISO_REL,
     "if not _ancestors_not_symlink(session.real_cwd, rel):",
     "if False:", (U2, U3)),
    ("devolucao-sinais", ISO_REL,
     "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})",
     "signal.pthread_sigmask(signal.SIG_BLOCK, set())", (U2,)),
    ("devolucao-marca-conflito", ISO_REL,
     "session.tainted = True",
     "session.tainted = False", (U2, U3)),
    ("rejeitado-sensivel", ISO_REL,
     "if any(deps.is_sensitive(p) for p in rel_slot.split(os.sep)):",
     "if False:", (U2,)),
    ("rejeitado-escapa", ISO_REL,
     "if not abs_path.startswith(session.slot + os.sep):",
     "if False:", (U2,)),
    ("gc-idade", ISO_REL,
     "velho = now - os.stat(lock_path).st_mtime > max_age",
     "velho = True", (U2, U3)),
    ("gc-symlink", ISO_REL,
     "if os.path.islink(path):\n"
     "        return False\n"
     "    if not os.path.realpath(path)",
     "if False:\n"
     "        return False\n"
     "    if not os.path.realpath(path)",
     (U3,)),
    ("gc-guarda", ISO_REL,
     "guard_slot(slot, common, deps)",
     "pass", (U3,)),
    ("prompt-uma-passada", ISO_REL,
     "return re.sub(pattern, lambda _match: slot_b, prompt)",
     "return re.sub(pattern, lambda _match: slot_b, "
     "re.sub(pattern, lambda _match: slot_b, prompt))", (U1, U3)),
    ("devolve-sempre", ROUTER_REL,
     "                _iso_end(attempt, allowed)",
     "                pass", (E,)),
    ("conflito-para", ROUTER_REL,
     "if sync is not None and sync.conflicts:",
     "if False:", (E,)),
    ("nivel-write-so-escrita", ROUTER_REL,
     'if level == "all" or (level == "write" and mode == "write"):',
     'if level in ("all", "write"):', (E,)),
    ("strict", ROUTER_REL,
     'if os.environ.get("WORKER_ASK_ISOLATION_STRICT") == "1":',
     "if False:", (E,)),
    ("marca-ambiente", ROUTER_REL,
     'env["WORKER_ASK_ISOLATED"] = "1"',
     "pass", (E,)),
    ("aviso-arvore-real", ROUTER_REL,
     "if sobra:",
     "if False:", (E,)),
    ("nf-sessao", ROUTER_REL,
     'if attempt is None or _ISO is None or mode != "write":',
     'if attempt is None or mode != "write":', (F,)),
    ("nf-modo", ROUTER_REL,
     'if attempt is None or _ISO is None or mode != "write":',
     "if attempt is None or _ISO is None:", (F,)),
    ("nf-chave", ROUTER_REL,
     'if os.environ.get("WORKER_ASK_STATE_FALLBACK") != "1" '
     "or name not in _STATE_FALLBACK_EXECUTORS:",
     "if name not in _STATE_FALLBACK_EXECUTORS:", (F,)),
    ("nf-executor", ROUTER_REL,
     'if os.environ.get("WORKER_ASK_STATE_FALLBACK") != "1" '
     "or name not in _STATE_FALLBACK_EXECUTORS:",
     'if os.environ.get("WORKER_ASK_STATE_FALLBACK") != "1":', (F,)),
    ("nf-marcador", ROUTER_REL,
     'if marker == b"launcher_incompatible_effects" or time.monotonic() >= deadline:',
     "if time.monotonic() >= deadline:", (F,)),
    ("nf-prazo", ROUTER_REL,
     'if marker == b"launcher_incompatible_effects" or time.monotonic() >= deadline:',
     'if marker == b"launcher_incompatible_effects":', (F,)),
    ("nf-devolvido", ROUTER_REL,
     "if sync is None or sync.applied or sync.conflicts:",
     "if sync is None or sync.conflicts:", (F,)),
    ("nf-conflito", ROUTER_REL,
     "if sync is None or sync.applied or sync.conflicts:",
     "if sync is None or sync.applied:", (F,)),
    ("nf-impressao-ausente", ROUTER_REL,
     'if attempt["fp_before"] is None or attempt["fp_after"] is None:',
     "if False:", (F,)),
    ("nf-impressao-diferente", ROUTER_REL,
     'if attempt["fp_before"] != attempt["fp_after"]:',
     "if False:", (F,)),
    ("nf-copia-mudou", ROUTER_REL,
     'if snapshot_allowed(attempt["cwd"], allowed) != attempt["prep"].snap_slot_before:',
     "if False:", (F,)),
    ("nf-real-mudou", ROUTER_REL,
     "return snapshot_allowed(cwd, allowed) == snap_before",
     "return True", (F,)),
    ("nf-so-rc1", ROUTER_REL,
     "if rc == 1 and _iso_no_effect(",
     "if _iso_no_effect(", (F,)),
)


def _base_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


def _repo_files():
    """Arquivos rastreados pelo git mais os nao rastreados (novos) sem ignorados."""
    try:
        raw = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"BLOCK: git ls-files indisponivel para copiar o repo ({exc})")
        return None
    return [item for item in raw.decode().split("\0") if item]


def _clean_pycache(*paths):
    for path in paths:
        shutil.rmtree(Path(path).parent / "__pycache__", ignore_errors=True)


def _pytest(stage, test_files, timeout):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *test_files],
        cwd=stage,
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _stage_sources(stage, files):
    """Copia o repo para a area temporaria; devolve (conteudos originais, caminhos alvo)."""
    for relative in files:
        origin = ROOT / relative
        if not origin.is_file():
            continue
        dest = stage / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, dest)
    originals = {}
    for relative in SOURCES:
        originals[relative] = (stage / relative).read_text()
    return originals


def _write_sources(stage, originals, mutant=None):
    """Escreve os dois fontes na copia: originais, com o mutante aplicado quando dado."""
    for relative, text in originals.items():
        if mutant is not None and mutant["source"] == relative:
            text = text.replace(mutant["old"], mutant["new"], 1)
        (stage / relative).write_text(text)


def mutation():
    files = _repo_files()
    if files is None:
        return 2
    originals_on_disk = {}
    for relative in SOURCES:
        originals_on_disk[relative] = (ROOT / relative).read_text()
    for name, source_rel, old, _new, _tests in MUTATIONS:
        if originals_on_disk[source_rel].count(old) != 1:
            print(f"BLOCK: mutante {name}: alvo nao e unico no fonte atual")
            return 2
    baseline_tests = sorted({t for m in MUTATIONS for t in m[4]})
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix="isolamento-mutation-") as directory:
            stage = Path(directory)
            originals = _stage_sources(stage, files)
            _write_sources(stage, originals)
            _clean_pycache(stage / ISO_REL, stage / ROUTER_REL)
            try:
                baseline = _pytest(stage, baseline_tests, BASELINE_TIMEOUT)
            except subprocess.TimeoutExpired:
                print("BLOCK: timeout de ferramenta na baseline")
                return 2
            if baseline.returncode != 0:
                print(f"BLOCK: baseline falhou ({baseline.returncode}); nao conta como morte")
                print(baseline.stdout[-2000:])
                print(baseline.stderr[-2000:])
                return 2
            for name, source_rel, old, new, tests in MUTATIONS:
                mutant = {"source": source_rel, "old": old, "new": new}
                try:
                    _write_sources(stage, originals, mutant)
                    _clean_pycache(stage / ISO_REL, stage / ROUTER_REL)
                    changed = _pytest(stage, tests, PYTEST_TIMEOUT)
                except subprocess.TimeoutExpired:
                    print(f"BLOCK: timeout de ferramenta no mutante {name}")
                    return 2
                if changed.returncode not in (0, 1) or "ERROR collecting" in changed.stdout:
                    # rc fora de 0/1 ou erro de coleta é infraestrutura, não veredito:
                    # não vale como morte nem como sobrevivência do mutante.
                    print(f"BLOCK: falha de infraestrutura no mutante {name} (rc {changed.returncode})")
                    print(changed.stdout[-2000:])
                    print(changed.stderr[-2000:])
                    return 2
                killed = changed.returncode == 1 and "FAILED" in changed.stdout
                results.append({"name": name, "source": source_rel, "tests": list(tests),
                                "killed": killed})
                print(f"{name} {'killed' if killed else 'survived'}", flush=True)
            _write_sources(stage, originals)
    except OSError as exc:
        print(f"BLOCK: falha de I/O na copia temporaria ({exc})")
        return 2
    score = 100 * sum(x["killed"] for x in results) / len(results)
    report = {
        "scope": "37 mutants do delta de isolamento (worker_isolation.py e worker_router.py); "
                 "nao exaustivo",
        "sources": list(SOURCES),
        "baseline_tests": baseline_tests,
        "results": results,
        "mutation_score_pct": round(score, 2),
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
    if score < MUTATION_MIN_DEFAULT:
        print(f"BLOCK: nota de mutacao {score:.2f}% abaixo do minimo ({MUTATION_MIN_DEFAULT:.0f}%)")
        return 1
    return 0


def _derived_rcfile():
    """Rcfile de cobertura do isolamento, gravado dentro de out-isolamento."""
    OUT.mkdir(exist_ok=True)
    rcfile = OUT / "coverage-isolamento.rc"
    rcfile.write_text(
        "[run]\n"
        "branch = True\n"
        f"include = */{ISO_REL}\n"
        f"data_file = {OUT}/.coverage-isolamento\n"
    )
    return rcfile


def _coverage_step(argv, env, timeout):
    try:
        return subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"BLOCK: falha de ferramenta em {' '.join(argv[:3])} ({exc})")
        return None


def coverage(line_min, branch_min):
    tests = [t for t in COVERAGE_TESTS if (ROOT / t).is_file()]
    if not tests:
        print("BLOCK: nenhum teste de isolamento existente para medir cobertura")
        return 2
    rcfile = _derived_rcfile()
    if rcfile is None:
        return 2
    env = _base_env()
    env["COVERAGE_RCFILE"] = str(rcfile)
    steps = [
        ([sys.executable, "-m", "coverage", "erase"], PYTEST_TIMEOUT),
        ([sys.executable, "-m", "coverage", "run", "--branch", "-m", "pytest", "-q", *tests],
         COVERAGE_PYTEST_TIMEOUT),
        ([sys.executable, "-m", "coverage", "json", "-o", str(OUT / "coverage.json")],
         PYTEST_TIMEOUT),
    ]
    for argv, timeout in steps:
        result = _coverage_step(argv, env, timeout)
        if result is None:
            return 2
        if result.returncode != 0:
            print(f"BLOCK: {' '.join(argv[:3])} retornou {result.returncode}")
            print(result.stdout[-2000:])
            print(result.stderr[-2000:])
            return 1
    try:
        data = json.loads((OUT / "coverage.json").read_text())
        key = next(k for k in data["files"] if k.endswith(ISO_REL))
        entry = data["files"][key]
        executed = set(entry["executed_lines"])
        missing = set(entry["missing_lines"])
        good = entry["executed_branches"]
        bad = entry["missing_branches"]
    except (OSError, ValueError, KeyError, StopIteration, TypeError) as exc:
        print(f"BLOCK: coverage.json sem o modulo do escopo ou formato inesperado ({exc})")
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
        "note": "measures the whole worker_isolation.py file; not ranges",
        "source": ISO_REL,
        "test": tests,
        "rcfile": "out-isolamento/coverage-isolamento.rc (branch, include, data_file)",
        "missing_lines": sorted(missing),
        "missing_branches": [list(b) for b in bad],
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
        print(f"BLOCK: cobertura do arquivo abaixo do minimo ({line_min}/{branch_min})")
        return 1
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] not in ("mutation", "coverage"):
        print("uso: check_isolation.py mutation|coverage [--line-min X --branch-min Y]")
        return 2
    if argv[1] == "mutation":
        if len(argv) != 2:
            print("uso: check_isolation.py mutation")
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
        print("uso: check_isolation.py coverage [--line-min X --branch-min Y]")
        return 2
    return coverage(line_min, branch_min)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
