"""Valida a remocao do modo --local do worker_router (cobertura + mutacao).

Uso (executado pelo orquestrador, com shell):
  python scripts/verificar_router_sem_local.py cobertura
  python scripts/verificar_router_sem_local.py mutacao
"""
import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out-router-sem-local"
COV_FILE = (OUT / ".coverage").resolve()
COV_JSON = OUT / "coverage.json"
SCOPE_JSON = OUT / "coverage-scope.json"
JACOCO_XML = OUT / "target" / "site" / "jacoco" / "jacoco.xml"
PIT_XML = OUT / "target" / "pit-reports" / "mutations.xml"
MUT_JSON = OUT / "mutation.json"
TESTS = [
    "tests/test_codex_local_router.py",
    "tests/test_worker_router.py",
    "tests/test_runs_log.py",
    "tests/test_presentation_worker.py",
]
MUT_TEST = "tests/test_codex_local_router.py"
MUT_SELECT = "flags_locais_recusadas"
MUTANTS = [
    ("local-flag", "parser.add_argument('--local', action='store_true')"),
    ("local-model", "parser.add_argument('--local-model', default=None)"),
    ("local-base-url", "parser.add_argument('--local-base-url', default=None)"),
]
COPY_FILES = [
    "worker_router.py",
    "quota_provider.py",
    "quota_reset.py",
    "pyproject.toml",
    MUT_TEST,
]
BAD_TOKENS = (
    "ModuleNotFoundError", "ImportError", "SyntaxError",
    "ERROR", "collected 0 items", "no tests ran",
)


def _run(cmd, env_extra=None, cwd=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or str(ROOT), env=env)
    return p


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _scope_ranges(path):
    """Ranges do delta roteamento/contrato CLI via AST (sem filtrar cobertura)."""
    text = Path(path).read_text()
    tree = ast.parse(text)
    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(node.name, node)
    ranges = []
    for fname in ("wrapper_chain", "build_argv"):
        node = funcs.get(fname)
        if node is not None:
            ranges.append({"function": fname, "start": node.lineno,
                           "end": node.end_lineno or node.lineno})
    impl = funcs.get("_main_impl")
    if impl is not None:
        assigns, calls = [], []
        for node in ast.walk(impl):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                targets = None
            if targets is not None and any(
                    isinstance(t, ast.Name) and t.id == "parser" for t in targets):
                assigns.append(node)
            if isinstance(node, ast.Call):
                func = node.func
                attr = ""
                if isinstance(func, ast.Attribute):
                    attr = func.attr
                elif isinstance(func, ast.Name):
                    attr = func.id
                if attr == "parse_args":
                    calls.append(node)
        if assigns and calls:
            start = min(n.lineno for n in assigns)
            end = max((n.end_lineno or n.lineno) for n in calls)
            ranges.append({"function": "_main_impl:argparse",
                           "start": start, "end": end})
    return ranges


def _norm_arcs(value):
    arcs = []
    for item in value or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            arcs.append([int(item[0]), int(item[1])])
        elif isinstance(item, dict) and "from" in item and "to" in item:
            arcs.append([int(item["from"]), int(item["to"])])
    return arcs


def cmd_cobertura():
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob(".coverage*"):
        stale.unlink()
    env = {"COVERAGE_FILE": str(COV_FILE)}
    r = _run([sys.executable, "-m", "coverage", "run", "--rcfile=coverage.ini",
              "-m", "pytest", *TESTS, "-q"], env_extra=env)
    sys.stdout.write(r.stdout[-4000:])
    sys.stderr.write(r.stderr[-4000:])
    if r.returncode != 0:
        print(f"cobertura: pytest sob coverage falhou rc={r.returncode}")
        return 1
    r = _run([sys.executable, "-m", "coverage", "combine", "--rcfile=coverage.ini"],
             env_extra=env)
    if r.returncode != 0:
        print("cobertura: coverage combine falhou; sem dados paralelos?")
        sys.stdout.write(r.stdout[-2000:])
    r = _run([sys.executable, "-m", "coverage", "json", "-o", str(COV_JSON),
              "--rcfile=coverage.ini"], env_extra=env)
    if r.returncode != 0:
        print("cobertura: coverage json falhou")
        sys.stdout.write(r.stdout[-2000:])
        return 1
    data = json.loads(COV_JSON.read_text())
    target = None
    for name in data.get("files", {}):
        if name.endswith("worker_router.py"):
            target = name
            break
    if target is None:
        print("cobertura: worker_router.py ausente no coverage.json")
        return 1
    entry = data["files"][target]
    s = entry.get("summary", {})
    cov_l = float(s.get("percent_covered", 0.0))
    n_stm, cov_stm = s.get("num_statements", 0), s.get("covered_lines", 0)
    n_br, cov_br = s.get("num_branches", 0), s.get("covered_branches", 0)
    print(f"worker_router.py global: linhas {cov_stm}/{n_stm} ({cov_l:.2f}%) "
          f"ramos {cov_br}/{n_br}")
    src = ROOT / "worker_router.py"
    ranges = _scope_ranges(src)
    if not ranges:
        print("cobertura: escopo vazio (AST nao encontrou funcoes/range)")
        return 2
    scope_lines = set()
    for rg in ranges:
        scope_lines.update(range(rg["start"], rg["end"] + 1))
    executed = set(entry.get("executed_lines", []))
    missing = set(entry.get("missing_lines", []))
    scope_cov_lines = sorted(executed & scope_lines)
    scope_miss_lines = sorted(missing & scope_lines)
    n_scope = len(scope_cov_lines) + len(scope_miss_lines)
    if n_scope == 0:
        print("cobertura: escopo sem statements executaveis")
        return 2
    exec_arcs = _norm_arcs(entry.get("executed_arcs",
                                     entry.get("executed_branches", [])))
    miss_arcs = _norm_arcs(entry.get("missing_arcs",
                                     entry.get("missing_branches", [])))
    scope_cov_br = sum(1 for a, _b in exec_arcs if a in scope_lines)
    scope_miss_arcs = [arc for arc in miss_arcs if arc[0] in scope_lines]
    scope_miss_br = len(scope_miss_arcs)
    n_scope_br = scope_cov_br + scope_miss_br
    pct_scope = 100.0 * len(scope_cov_lines) / n_scope
    pct_scope_br = (100.0 * scope_cov_br / n_scope_br) if n_scope_br else 100.0
    print(f"escopo delta: linhas {len(scope_cov_lines)}/{n_scope} "
          f"({pct_scope:.2f}%) ramos {scope_cov_br}/{n_scope_br} "
          f"({pct_scope_br:.2f}%)")
    scope_payload = {
        "source": "worker_router.py",
        "source_sha256": _sha256(src),
        "coverage_json_sha256": _sha256(COV_JSON),
        "ranges": ranges,
        "global": {"lines_covered": cov_stm, "lines_total": n_stm,
                   "branches_covered": cov_br, "branches_total": n_br},
        "scope": {"lines_covered": len(scope_cov_lines),
                  "lines_total": n_scope,
                  "percent_lines": pct_scope,
                  "branches_covered": scope_cov_br,
                  "branches_total": n_scope_br,
                  "percent_branches": pct_scope_br,
                  "missing_lines": scope_miss_lines,
                  "missing_branches": scope_miss_arcs},
    }
    SCOPE_JSON.write_text(json.dumps(scope_payload, indent=2))
    JACOCO_XML.parent.mkdir(parents=True, exist_ok=True)
    rep = ET.Element("report", {"name": "router-sem-local"})
    sf = rep  # O gate lê os contadores agregados na raiz do XML.
    ET.SubElement(sf, "counter", {"type": "LINE",
                                 "missed": str(len(scope_miss_lines)),
                                 "covered": str(len(scope_cov_lines))})
    ET.SubElement(sf, "counter", {"type": "BRANCH",
                                 "missed": str(scope_miss_br),
                                 "covered": str(scope_cov_br)})
    ET.ElementTree(rep).write(JACOCO_XML, encoding="utf-8", xml_declaration=True)
    print(f"json: {COV_JSON.relative_to(ROOT)} "
          f"scope: {SCOPE_JSON.relative_to(ROOT)} "
          f"xml: {JACOCO_XML.relative_to(ROOT)}")
    return 0


def _clean_pycache(base):
    for p in Path(base).rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    for p in Path(base).rglob("*.pyc"):
        try:
            p.unlink()
        except OSError:
            pass


def _stage(tmp):
    base = Path(tmp)
    for rel in COPY_FILES:
        src = ROOT / rel
        dst = base / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    helper = ROOT / "bin" / "ask_presentation.py"
    if helper.is_file():
        dst = base / "bin" / "ask_presentation.py"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(helper, dst)


def _pytest_select(base):
    return [sys.executable, "-m", "pytest", MUT_TEST, "-k", MUT_SELECT, "-q"]


def cmd_mutacao():
    src = ROOT / "worker_router.py"
    text = src.read_text()
    anchor = next((ln for ln in text.splitlines()
                   if "add_argument" in ln and "--cwd" in ln), None)
    if anchor is None:
        print("mutacao: ancora parser.add_argument('--cwd') ausente")
        return 2
    indent = anchor[: len(anchor) - len(anchor.lstrip())]
    tmp = tempfile.mkdtemp(prefix="router-sem-local-")
    try:
        _stage(tmp)
        base = Path(tmp)
        rb = _run(_pytest_select(tmp), cwd=tmp)
        out_b = (rb.stdout + rb.stderr)[-4000:]
        if rb.returncode != 0:
            print(f"mutacao: baseline falhou rc={rb.returncode}")
            sys.stdout.write(out_b)
            return 2
        results, killed = [], 0
        orig_base = (base / "worker_router.py").read_text()
        for name, stmt in MUTANTS:
            staged = base / "worker_router.py"
            mutated = orig_base.replace(anchor, indent + stmt + "\n" + anchor, 1)
            staged.write_text(mutated)
            _clean_pycache(tmp)
            r = _run(_pytest_select(tmp), cwd=tmp)
            blob = r.stdout + r.stderr
            failed = "FAILED" in blob
            bad = any(t in blob for t in BAD_TOKENS)
            dead = r.returncode == 1 and failed and not bad
            killed += 1 if dead else 0
            results.append({"mutant": name, "stmt": stmt,
                            "returncode": r.returncode, "killed": dead,
                            "tail": blob[-1200:]})
            status = "KILLED" if dead else "SURVIVED"
            print(f"mutante {name}: {status} (rc={r.returncode})")
        score = killed / len(MUTANTS)
        print(f"mutacao: {killed}/{len(MUTANTS)} mortos score={score:.2f}")
        for res in results:
            if not res["killed"]:
                print(f"sobrevivente: {res['mutant']} rc={res['returncode']}")
        OUT.mkdir(parents=True, exist_ok=True)
        PIT_XML.parent.mkdir(parents=True, exist_ok=True)
        rep = ET.Element("mutations")
        for res in results:
            ET.SubElement(rep, "mutation", {
                "id": res["mutant"], "detected": str(res["killed"]).lower(),
                "returncode": str(res["returncode"]), "stmt": res["stmt"]})
        ET.ElementTree(rep).write(PIT_XML, encoding="utf-8", xml_declaration=True)
        MUT_JSON.write_text(json.dumps({
            "source_sha256": _sha256(src),
            "test_sha256": _sha256(ROOT / MUT_TEST),
            "baseline": "passed",
            "mutants": [{k: v for k, v in m.items() if k != "tail"}
                        for m in results],
            "killed": killed, "total": len(MUTANTS), "score": score,
        }, indent=2, ensure_ascii=False))
        print(f"xml: {PIT_XML.relative_to(ROOT)} json: {MUT_JSON.relative_to(ROOT)}")
        return 0 if killed == len(MUTANTS) else 1
    finally:
        _clean_pycache(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["cobertura", "mutacao"])
    args = ap.parse_args(argv)
    if args.action == "cobertura":
        return cmd_cobertura()
    return cmd_mutacao()


if __name__ == "__main__":
    raise SystemExit(main())
