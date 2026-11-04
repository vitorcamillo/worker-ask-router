#!/usr/bin/env python3
"""Mutacao direcionada do codex-local; amostra explicita, nunca exaustiva.

Sete guardas reais do runtime, cada uma com selecao -k real. Se qualquer
guarda nao for encontrada exatamente uma vez no runtime, o script sai com
2 (BLOCKED) em vez de pular silenciosamente. Baseline com rc5 (nada
coletado) tambem bloqueia: selecao inexistente nao e aprovacao.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "bin" / "codex-local"
TESTS = ROOT / "tests" / "test_codex_local.py"
EXAMPLE = ROOT / "docs" / "codex-local-spec.example.json"
OUT = ROOT / "out" / "codex-local"
PIT_XML = ROOT / "out" / "target" / "pit-reports" / "mutations.xml"

EXPLICIT = [
    ("local-only-host",
     'if host not in ("127.0.0.1", "localhost", "::1"):',
     "if False:",
     "given_remote_url_when_preflight_then_rejects_before_model"
     " or given_base_url_when_validated_then_normalizes"),
    ("spec-unknown-keys",
     "if k not in SPEC_KEYS:",
     "if False:",
     "given_bad_specs_when_validated_then_rc2_before_model"),
    ("spec-scope-abs",
     'if value.startswith(("/", "\\\\")) or _DRIVE_RE.match(value):',
     "if False:",
     "spec_field_boundaries"),
    ("spec-scope-dotdot",
     'if comp in ("", ".", ".."):',
     "if False:",
     "spec_field_boundaries"),
    ("context-cap",
     "if not (MIN_CTX <= ctx_req <= MAX_CTX):",
     "if False:",
     "given_context_above32k_when_validated_then_rejects"),
    ("spec-budget",
     "if spec_len > budget:",
     "if False:",
     "given_spec_over_budget_when_validated_then_rejects_before_inference"),
    ("final-scope-subset",
     "if norm not in allowed:",
     "if False:",
     "given_bad_finals_when_validated_then_no_success"),
]


def collect_mutations(source):
    found = []
    for name, old, new, selection in EXPLICIT:
        count = source.count(old)
        if count != 1:
            print(f"guarda '{name}' encontrada {count}x (esperado 1x); "
                  "runtime divergiu, mutacao bloqueada", file=sys.stderr)
            return None
        found.append({"name": name, "old": old, "new": new,
                      "selection": selection})
    return found


def stage_minimal(stage):
    (stage / "tests").mkdir(parents=True, exist_ok=True)
    (stage / "bin").mkdir(parents=True, exist_ok=True)
    (stage / "docs").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, stage / "bin" / "codex-local")
    shutil.copy2(TESTS, stage / "tests" / "test_codex_local.py")
    if EXAMPLE.exists():
        shutil.copy2(EXAMPLE, stage / "docs" / "codex-local-spec.example.json")
    if (ROOT / "pyproject.toml").exists():
        shutil.copy2(ROOT / "pyproject.toml", stage / "pyproject.toml")


def clean_pycache(stage):
    for sub in ("bin", "tests"):
        shutil.rmtree(stage / sub / "__pycache__", ignore_errors=True)


def run_pytest(stage, env, selection):
    argv = [sys.executable, "-m", "pytest", "-q",
            "tests/test_codex_local.py", "-k", selection]
    return subprocess.run(argv, cwd=stage, env=env, capture_output=True,
                          text=True, timeout=300)


def main():
    if not SOURCE.exists():
        print("bin/codex-local ausente; mutacao bloqueada", file=sys.stderr)
        return 2
    source = SOURCE.read_text(encoding="utf-8")
    mutations = collect_mutations(source)
    if mutations is None:
        return 2
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("COVERAGE_")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    results = []
    with tempfile.TemporaryDirectory(prefix="codex-local-mutation-") as directory:
        stage = Path(directory)
        stage_minimal(stage)
        for mut in mutations:
            target = stage / "bin" / "codex-local"
            current = SOURCE.read_text(encoding="utf-8")
            baseline = run_pytest(stage, env, mut["selection"])
            if baseline.returncode == 5:
                print(f"baseline rc5 (nada coletado) para {mut['name']}; "
                      "selecao invalida, mutacao bloqueada", file=sys.stderr)
                return 2
            if baseline.returncode != 0:
                print(f"baseline falhou para {mut['name']}", file=sys.stderr)
                print(baseline.stdout[-2000:], file=sys.stderr)
                return 2
            target.write_text(current.replace(mut["old"], mut["new"], 1),
                              encoding="utf-8")
            clean_pycache(stage)
            mutant = run_pytest(stage, env, mut["selection"])
            killed = (mutant.returncode == 1 and "FAILED" in mutant.stdout
                      and "ERROR collecting" not in mutant.stdout)
            results.append({"name": mut["name"],
                            "selection": mut["selection"],
                            "killed": killed,
                            "exit_code": mutant.returncode})
            print(mut["name"], "killed" if killed else "survived", flush=True)
            target.write_text(current, encoding="utf-8")
            clean_pycache(stage)
    sha = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    score = 100.0 * sum(1 for r in results if r["killed"]) / len(results)
    report = {"scope": ("amostra direcionada codex-local (7 guardas): "
                        "host local-only, chaves desconhecidas, escopo abs, "
                        "dotdot, teto de contexto, orcamento de bytes, "
                        "subset changed_files; nao exaustivo"),
              "source_sha256": sha,
              "tests_sha256": hashlib.sha256(TESTS.read_bytes()).hexdigest(),
              "results": results, "mutation_score_pct": round(score, 2),
              "total": len(results),
              "killed": sum(1 for r in results if r["killed"])}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "mutation.json").write_text(json.dumps(report, indent=2) + "\n",
                                       encoding="utf-8")
    xml = ET.Element("mutations")
    for item in results:
        ET.SubElement(xml, "mutation", name=item["name"],
                      detected=str(item["killed"]).lower(),
                      status="KILLED" if item["killed"] else "SURVIVED")
    PIT_XML.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(xml).write(PIT_XML, encoding="unicode")
    return 0 if all(r["killed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
