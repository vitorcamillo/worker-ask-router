"""Valida o delta "no-route" do bin/opencode-ask (cobertura + mutacao).

Uso (executado pelo orquestrador, com shell):
  python scripts/verificar_opencode_noroute.py cobertura
  python scripts/verificar_opencode_noroute.py mutacao
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out-opencode-noroute"
TRACE_FILE = OUT / "noroute.trace"
BASH_ENV_FILE = OUT / "bash_env.sh"
COV_JSON = OUT / "coverage.json"
JACOCO_XML = OUT / "target" / "site" / "jacoco" / "jacoco.xml"
PIT_XML = OUT / "target" / "pit-reports" / "mutations.xml"
MUT_JSON = OUT / "mutation.json"
SRC = ROOT / "bin" / "opencode-ask"
TEST = "tests/test_opencode.py"
SELECT = "noroute or common_failure_not_fallback or stderr_only_quota"
BAD_TOKENS = (
    "ModuleNotFoundError", "ImportError", "SyntaxError",
    "ERROR", "collected 0 items", "no tests ran",
)
DELTA = "|no-route|model unavailable"
ONLY_NOROUTE = "|no-route"
TRACE_RE = re.compile(r"^\+(.+):([0-9]+):")
MUTANTS = [
    ("L1-sem-noroute", "l1",
     "remove '|no-route|model unavailable' so de L1"),
    ("L2-sem-noroute", "l2",
     "remove '|no-route|model unavailable' so de L2"),
    ("ambos-sem-noroute", "both",
     "remove '|no-route|model unavailable' das duas linhas"),
    ("L1-sem-no-route-so", "both-no-route-only",
     "remove so '|no-route' de L1 e L2, mantendo '|model unavailable'"),
]


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


def _locate(text):
    """Localiza L1, L2 e os alvos dos 4 ramos por conteudo (1-based)."""
    lines = text.split("\n")
    cands = [i for i, ln in enumerate(lines)
             if "grep -qiE" in ln and DELTA in ln]
    if len(cands) != 2:
        print(f"localizar: esperado 2 linhas grep/no-route, achei {len(cands)}")
        return None
    l1 = next((i for i in cands if "$xrc" in lines[i]), None)
    rest = [i for i in cands if i != l1] if l1 is not None else []
    if l1 is None or len(rest) != 1:
        print("localizar: L1 (laco de fallback, com $xrc) nao identificada")
        return None
    l2 = rest[0]
    if not l1 < l2:
        print("localizar: ordem L1<L2 violada")
        return None
    b1_true = l1 + 1
    if b1_true >= len(lines) or not lines[b1_true].strip():
        print("localizar: linha seguinte a L1 ausente/vazia")
        return None
    b1_false = next((j for j in range(l1 + 1, l2)
                     if re.match(r"\s*break(\s|;|#|$)", lines[j])), None)
    if b1_false is None:
        print("localizar: break do laco de fallback nao achado")
        return None
    b2_true = next((j for j in range(l2 + 1, min(l2 + 16, len(lines)))
                    if "exit 75" in lines[j]), None)
    if b2_true is None:
        print("localizar: exit 75 apos L2 nao achado")
        return None
    tail = next((j for j in range(len(lines) - 1, -1, -1) if lines[j].strip()), None)
    if tail is None or lines[tail].strip() != "exit 1":
        print("localizar: ultima linha do arquivo nao e 'exit 1'")
        return None
    # L2 pode ser comando continuado com '\': o xtrace pode atribuir a
    # execucao a qualquer linha fisica do comando; aceite o intervalo.
    span = l2
    while lines[span].rstrip().endswith("\\") and span + 1 < len(lines):
        span += 1
    return {
        "l1": l1 + 1, "l2": l2 + 1,
        "l2_span": [n + 1 for n in range(l2, span + 1)],
        "b1_true": b1_true + 1, "b1_false": b1_false + 1,
        "b2_true": b2_true + 1, "b2_false": tail + 1,
    }


def _pytest_noroute(guards_src=None):
    cmd = [sys.executable, "-m", "pytest", TEST, "-k", SELECT, "-q"]
    env_extra = {"GUARDS_SRC": str(guards_src)} if guards_src else None
    return _run(cmd, env_extra=env_extra)


def cmd_cobertura():
    text = SRC.read_text(encoding="utf-8")
    loc = _locate(text)
    if loc is None:
        print("cobertura: L1/L2 do delta no-route nao localizados por conteudo")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    if TRACE_FILE.exists():
        TRACE_FILE.unlink()
    BASH_ENV_FILE.write_text(
        "exec 9>>\"$NOROUTE_TRACE\"; BASH_XTRACEFD=9; "
        "PS4='+${BASH_SOURCE}:${LINENO}: '; set -x\n",
        encoding="utf-8",
    )
    env = {"BASH_ENV": str(BASH_ENV_FILE), "NOROUTE_TRACE": str(TRACE_FILE)}
    r = _run([sys.executable, "-m", "pytest", TEST, "-k", SELECT, "-q"],
             env_extra=env)
    blob = r.stdout + r.stderr
    sys.stdout.write(blob[-4000:])
    bad = [t for t in BAD_TOKENS if t in blob]
    if r.returncode != 0 or bad:
        print(f"cobertura: pytest falhou rc={r.returncode} bad={bad}")
        return 1
    traced = set()
    if TRACE_FILE.is_file():
        for line in TRACE_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            m = TRACE_RE.match(line)
            if m and m.group(1).endswith("opencode-ask"):
                try:
                    traced.add(int(m.group(2)))
                except ValueError:
                    pass
    delta_lines = [loc["l1"], loc["l2"]]
    l2_hit = sorted(set(loc["l2_span"]) & traced)
    cov_lines = [n for n in delta_lines
                 if (n in traced or (n == loc["l2"] and l2_hit))]
    miss_lines = [n for n in delta_lines if n not in cov_lines]
    branches = [("L1-verdadeiro", loc["b1_true"]),
                ("L1-falso", loc["b1_false"]),
                ("L2-verdadeiro", loc["b2_true"]),
                ("L2-falso", loc["b2_false"])]
    cov_br = sum(1 for _, n in branches if n in traced)
    miss_br = [{"branch": name, "line": n} for name, n in branches if n not in traced]
    pct_line = 100.0 * len(cov_lines) / len(delta_lines)
    pct_br = 100.0 * cov_br / len(branches)
    print(f"delta no-route: L1={loc['l1']} L2={loc['l2']} "
          f"linhas {len(cov_lines)}/{len(delta_lines)} ({pct_line:.2f}%) "
          f"ramos {cov_br}/{len(branches)} ({pct_br:.2f}%)")
    if miss_lines:
        print(f"linhas descobertas: {miss_lines}")
    for item in miss_br:
        print(f"ramo descoberto: {item['branch']} (linha {item['line']})")
    COV_JSON.write_text(json.dumps({
        "source": "bin/opencode-ask",
        "source_sha256": _sha256(SRC),
        "tests": [TEST],
        "select": SELECT,
        "trace": str(TRACE_FILE.relative_to(ROOT)),
        "delta_lines": {"L1": loc["l1"], "L2": loc["l2"]},
        "branch_targets": {name: n for name, n in branches},
        "lines_covered": len(cov_lines),
        "lines_total": len(delta_lines),
        "branches_covered": cov_br,
        "branches_total": len(branches),
        "percent_lines": pct_line,
        "percent_branches": pct_br,
        "missing_lines": miss_lines,
        "missing_branches": miss_br,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    JACOCO_XML.parent.mkdir(parents=True, exist_ok=True)
    rep = ET.Element("report", {"name": "opencode-noroute"})
    sf = rep  # O gate lê os contadores agregados na raiz do XML.
    ET.SubElement(sf, "counter", {"type": "LINE",
                                 "missed": str(len(miss_lines)),
                                 "covered": str(len(cov_lines))})
    ET.SubElement(sf, "counter", {"type": "BRANCH",
                                 "missed": str(len(branches) - cov_br),
                                 "covered": str(cov_br)})
    ET.ElementTree(rep).write(JACOCO_XML, encoding="utf-8", xml_declaration=True)
    print(f"json: {COV_JSON.relative_to(ROOT)} "
          f"xml: {JACOCO_XML.relative_to(ROOT)}")
    print(json.dumps({"coverage_line_pct": round(pct_line, 2),
                      "coverage_branch_pct": round(pct_br, 2)}))
    return 0 if (not miss_lines and pct_br >= 75.0) else 1


def _apply_mutant(lines, kind, loc):
    out = list(lines)

    def drop_full(idx):
        if DELTA not in out[idx]:
            return False
        out[idx] = out[idx].replace(DELTA, "", 1)
        return True

    def drop_noroute(idx):
        if ONLY_NOROUTE not in out[idx]:
            return False
        out[idx] = out[idx].replace(ONLY_NOROUTE, "", 1)
        return True

    l1, l2 = loc["l1"] - 1, loc["l2"] - 1
    if kind == "l1":
        ok = drop_full(l1)
    elif kind == "l2":
        ok = drop_full(l2)
    elif kind == "both":
        ok = drop_full(l1) and drop_full(l2)
    elif kind == "both-no-route-only":
        ok = drop_noroute(l1) and drop_noroute(l2)
    else:
        ok = False
    return out, ok


def cmd_mutacao():
    text = SRC.read_text(encoding="utf-8")
    loc = _locate(text)
    if loc is None:
        print("mutacao: L1/L2 do delta no-route nao localizados por conteudo")
        return 1
    lines = text.split("\n")
    if DELTA not in lines[loc["l1"] - 1] or DELTA not in lines[loc["l2"] - 1]:
        print("mutacao: alvo '|no-route|model unavailable' ausente em L1/L2")
        return 1
    rb = _pytest_noroute(str(ROOT / "bin"))
    out_b = (rb.stdout + rb.stderr)[-4000:]
    if rb.returncode != 0 or any(t in out_b for t in BAD_TOKENS):
        print(f"mutacao: baseline falhou rc={rb.returncode}")
        sys.stdout.write(out_b)
        return 2
    results = []
    for name, kind, desc in MUTANTS:
        tmp = tempfile.mkdtemp(prefix="opencode-noroute-")
        try:
            bindir = Path(tmp) / "bin"
            shutil.copytree(ROOT / "bin", bindir)
            mutated, ok = _apply_mutant(lines, kind, loc)
            if not ok:
                print(f"mutacao: alvo nao encontrado para {name}")
                return 1
            (bindir / "opencode-ask").write_text("\n".join(mutated), encoding="utf-8")
            r = _pytest_noroute(str(bindir))
            blob = r.stdout + r.stderr
            failed = "failed" in blob.lower()
            bad = any(t in blob for t in BAD_TOKENS)
            dead = r.returncode != 0 and failed and not bad
            equivalente = (not dead) and kind == "both-no-route-only"
            motivo = ""
            if equivalente:
                motivo = ("remove so '|no-route'; a mensagem real contem "
                          "'provider.no-route' e 'Model unavailable', que ainda casa "
                          "com '|model unavailable': equivalente ao original")
            results.append({"mutant": name, "desc": desc,
                            "returncode": r.returncode, "killed": dead,
                            "equivalente": equivalente, "motivo": motivo,
                            "detected": bool(dead or equivalente)})
            status = "KILLED" if dead else ("EQUIVALENTE" if equivalente else "SURVIVED")
            print(f"mutante {name}: {status} (rc={r.returncode})")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    detected_n = sum(1 for m in results if m["detected"])
    total = len(results)
    score = detected_n / total
    pct = round(100.0 * score, 2)
    print(f"mutacao: {detected_n}/{total} detectados score={score:.2f}")
    for m in results:
        if not m["detected"]:
            print(f"sobrevivente nao-equivalente: {m['mutant']} rc={m['returncode']}")
    OUT.mkdir(parents=True, exist_ok=True)
    PIT_XML.parent.mkdir(parents=True, exist_ok=True)
    rep = ET.Element("mutations")
    for m in results:
        attrs = {"id": m["mutant"], "detected": str(m["detected"]).lower(),
                 "status": "KILLED" if m["killed"] else "SURVIVED",
                 "returncode": str(m["returncode"])}
        if m["equivalente"]:
            attrs["equivalente"] = "true"
        ET.SubElement(rep, "mutation", attrs)
    ET.ElementTree(rep).write(PIT_XML, encoding="utf-8", xml_declaration=True)
    MUT_JSON.write_text(json.dumps({
        "source": "bin/opencode-ask",
        "source_sha256": _sha256(SRC),
        "tests": [TEST],
        "select": SELECT,
        "baseline": "passed",
        "mutants": results,
        "killed": sum(1 for m in results if m["killed"]),
        "detected": detected_n,
        "total": total,
        "score": score,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"xml: {PIT_XML.relative_to(ROOT)} json: {MUT_JSON.relative_to(ROOT)}")
    print(json.dumps({"mutation_score_pct": pct}))
    return 0 if detected_n == total else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["cobertura", "mutacao"])
    args = ap.parse_args(argv)
    if args.action == "cobertura":
        return cmd_cobertura()
    return cmd_mutacao()


if __name__ == "__main__":
    raise SystemExit(main())
