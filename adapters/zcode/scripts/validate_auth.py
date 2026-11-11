#!/usr/bin/env python3
"""Validação executável da sessão candidata, sem rede, login ou segredos reais."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "auth"
NODE_TEST = ROOT / "tests" / "auth-session.test.cjs"
PY_TEST = ROOT / "tests" / "test_auth_session.py"
HELPER = ROOT / "auth" / "zai-session.cjs"
LAUNCHER = ROOT / "auth" / "launch.cjs"
CANDIDATE = ROOT / "bin" / "zcode-cli-auth-candidate"
REAL_BUNDLE = Path(os.environ.get("ZCODE_REAL_BUNDLE") or Path.home() / ".local" / "share" / "zcode-cli" / "glm" / "zcode.cjs")
CATEGORIES = ("unit", "gherkin", "quality", "metrics", "mutation", "coverage", "acceptance", "qa", "e2e")
SYNTHETIC_FIXTURE_FILES = {"tests/auth-session.test.cjs", "tests/test_auth_session.py"}
SUSPICIOUS_LITERAL_RULES = (
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----")),
    ("jwt_literal", re.compile(r"[\"']eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}[\"']")),
    ("credential_literal", re.compile(
        r"\b(?:access[_-]?token|refresh[_-]?token|jwt|api[_-]?key|password|secret|cookie)\b"
        r"\s*[:=]\s*[\"'][^\"']{12,}[\"']", re.I
    )),
)


def run(argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 120.0) -> dict:
    started = time.monotonic()
    merged = dict(os.environ)
    merged.update({"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": str(ROOT / "src")})
    if env:
        merged.update(env)
    try:
        completed = subprocess.run(
            argv, cwd=ROOT, env=merged, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        result = {"argv": argv, "returncode": completed.returncode,
                  "stdout": completed.stdout[-6000:], "stderr": completed.stderr[-6000:]}
    except (OSError, subprocess.TimeoutExpired) as error:
        result = {"argv": argv, "returncode": None, "error": type(error).__name__, "stdout": "", "stderr": ""}
    result["duration_s"] = round(time.monotonic() - started, 3)
    return result


def write(category: str, report: dict) -> int:
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    (OUT / f"{category}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report.get("status") == "PASS" else 1


def command_report(category: str, commands: list[dict]) -> int:
    status = "PASS" if commands and all(item.get("returncode") == 0 for item in commands) else "FAIL"
    return write(category, {"category": category, "status": status, "commands": commands,
                            "pass_rate_pct": round(100 * sum(item.get("returncode") == 0 for item in commands) / len(commands), 2) if commands else 0,
                            "evidence": [str(OUT / f"{category}.json")]})


def node_tests() -> dict:
    # Runner embutido diretamente para preservar a lista nominal de testes.
    return run(["node", str(NODE_TEST)])


def run_unit() -> int:
    return command_report("unit", [run([sys.executable, "-m", "pytest", "-q", str(PY_TEST)]), node_tests()])


def scenario_links() -> list[str]:
    text = (ROOT / "docs" / "auth-session.md").read_text(encoding="utf-8")
    return re.findall(r"^- Scenario: .+ — Test: `tests/auth-session\.test\.cjs` / `([^`]+)`$", text, re.M)


def executed_test_names(output: str) -> list[str]:
    return re.findall(r"^# Subtest: (.+)$", output, re.M)


def run_gherkin() -> int:
    result = node_tests()
    declared = scenario_links()
    executed = executed_test_names(result.get("stdout", ""))
    missing = [name for name in declared if name not in executed]
    matched = len(declared) - len(missing)
    return write("gherkin", {"category": "gherkin",
        "status": "PASS" if result.get("returncode") == 0 and declared and not missing else "FAIL",
        "scenarios_declared": declared, "scenarios_executed": executed, "missing": missing,
        "pass_rate_pct": round(100 * matched / len(declared), 2) if declared else 0,
        "command": result, "evidence": ["docs/auth-session.md", str(NODE_TEST)]})


def scan_secret_literals() -> list[dict]:
    findings = []
    for path in (HELPER, LAUNCHER, CANDIDATE, NODE_TEST, PY_TEST):
        relative = path.relative_to(ROOT).as_posix()
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for rule, pattern in SUSPICIOUS_LITERAL_RULES:
                if pattern.search(line) and not (relative in SYNTHETIC_FIXTURE_FILES and "synthetic-" in line):
                    findings.append({"file": relative, "line": line_number, "rule": rule})
    return findings


def run_quality() -> int:
    commands = [run(["node", "--check", str(HELPER)]), run(["node", "--check", str(LAUNCHER)]),
                run(["bash", "-n", str(CANDIDATE)]), run([sys.executable, "-m", "pytest", "-q", str(PY_TEST)])]
    findings = scan_secret_literals()
    return write("quality", {"category": "quality",
        "status": "PASS" if all(c.get("returncode") == 0 for c in commands) and not findings else "FAIL",
        "commands": commands, "secret_scan": {"status": "PASS" if not findings else "FAIL", "findings": findings},
        "evidence": [str(HELPER), str(LAUNCHER), str(CANDIDATE), str(PY_TEST), str(NODE_TEST)]})


def coverage_values(output: str) -> tuple[float | None, float | None]:
    match = re.search(r"^#\s+all files\s+\|\s+([0-9.]+)\s+\|\s+([0-9.]+)", output, re.I | re.M)
    return (float(match.group(1)), float(match.group(2))) if match else (None, None)


def run_coverage() -> int:
    result = run(["node", "--experimental-test-coverage", "--test-coverage-include=auth/*.cjs", str(NODE_TEST)], timeout=180)
    line, branch = coverage_values(result.get("stdout", "") + result.get("stderr", ""))
    status = "PASS" if result.get("returncode") == 0 and line is not None and branch is not None and line >= 85 and branch >= 75 else "FAIL"
    return write("coverage", {"category": "coverage", "status": status, "command": result,
        "metrics": {"coverage_line_pct": line, "coverage_branch_pct": branch}, "scope": ["auth/*.cjs"],
        "thresholds": {"coverage_line_pct": 85, "coverage_branch_pct": 75},
        "evidence": [str(HELPER), str(LAUNCHER), str(NODE_TEST)]})


def assertion_kill(result: dict) -> bool:
    output = f"{result.get('stdout', '')}\n{result.get('stderr', '')}"
    if result.get("returncode") == 0 or any(marker in output for marker in ("SyntaxError", "ERR_MODULE_NOT_FOUND", "TimeoutError", "timed out", "Cannot find module")):
        return False
    return "AssertionError" in output or "ERR_ASSERTION" in output


def run_mutation() -> int:
    baseline = [run([sys.executable, "-m", "pytest", "-q", str(PY_TEST)]), node_tests()]
    if not all(item.get("returncode") == 0 for item in baseline):
        return write("mutation", {"category": "mutation", "status": "BLOCKED",
            "reason": "baseline completo não está verde; mutantes não foram executados",
            "baseline": baseline, "mutants": [], "evidence": [str(NODE_TEST), str(PY_TEST)]})
    mutations = {
        "ttl_skew": ("const CACHE_SKEW_MS = 120_000;", "const CACHE_SKEW_MS = 0;"),
        "fingerprint_guard": ("existing.record.oauthFingerprint === oauthFingerprint", "true"),
        "response_code_validation": ("![0, 200, \"0\", \"200\"].includes(response.code)", "false"),
        "cas_delete": ("return await locationStore.deleteIfValue(STORAGE_KEY, loaded.raw);", "return true;"),
        "cas_create_absent": ("await store.saveIfValue(STORAGE_KEY, null, replacement);", "await store.save(STORAGE_KEY, replacement);"),
    }
    results = []
    with tempfile.TemporaryDirectory(prefix="zcode-auth-mutants-") as directory:
        for name, (old, new) in mutations.items():
            source = HELPER.read_text(encoding="utf-8")
            if old not in source:
                results.append({"name": name, "status": "GATE_FAIL", "reason": "mutation target absent"})
                continue
            mutant = Path(directory) / f"{name}.cjs"
            mutant.write_text(source.replace(old, new, 1), encoding="utf-8")
            result = run(["node", str(NODE_TEST)], env={"AUTH_SESSION_MODULE": str(mutant)})
            results.append({"name": name, "status": "KILLED" if assertion_kill(result) else "SURVIVED", "command": result})
    killed = sum(item.get("status") == "KILLED" for item in results)
    score = round(100 * killed / len(results), 2) if results else 0
    status = "PASS" if results and score >= 80 and all(item.get("status") == "KILLED" for item in results) else "FAIL"
    return write("mutation", {"category": "mutation", "status": status, "baseline": baseline,
        "mutants": results, "metrics": {"mutation_score_pct": score, "mutants_total": len(results), "mutants_killed": killed},
        "thresholds": {"mutation_score_pct": 80}, "evidence": [str(HELPER), str(NODE_TEST)]})


def fixture_bundle() -> str:
    # Nomes do bundle de 02/11/2026 de propósito: exercita o casamento por estrutura
    # (FLEX) do launcher; só a forma do CAS é a do 3.18.0. Referência: fixture
    # em tests/auth-session.test.cjs (linhas ~412–426 e ~495–506).
    return "\n".join([
        "const e={locationStore:null,httpClient:null,env:{},credentialStore:null};const h='synthetic-oauth';const n={resolve:async(_g,fn)=>fn(),invalidate:()=>{}};const o=async()=>({code:0,data:{access_token:'synthetic-business',expires_in:300}});const nativeStore={async deleteIfValue(i,a){let c=pj(i),u=OK(a),f=!1;return await Hq(async g=>{let w=g[c];w===void 0||await o.decrypt(w)!==u||(delete g[c],f=!0)}),f}};",
        "function Yht(){return n.resolve(h,()=>o(`${Kgo}/api/auth/z/login`,{method:\"POST\",headers:{\"Content-Type\":\"application/json\"},body:JSON.stringify({token:h})}))}",
        "function resolverMarker(e){return Yht({httpClient:e.httpClient??r0i(e.env)})}",
        "async function __storeAnchor(e){const v=e.credentialStore??KE({env:t}),g=await v.captureRecoveryGuard();return g;}",
        "function obc(e){return Promise.resolve({token:'synthetic-business'})}const Nae=async(value)=>value;async function Ooi(e){const b={saveZaiLoginCredentials:async()=>{}};const n='zai';const t={};const u=undefined;const v={loginStore:'fixture'};const R=await Promise.resolve({accessToken:'synthetic-oauth',token:'synthetic-jwt',user:{}}),N=await Nae(obc({accessToken:R.accessToken,env:t,httpClient:e.httpClient,family:n,resolver:e.apiKeyResolver,signal:u}),u);if(n==='zai')await b.saveZaiLoginCredentials({accessToken:R.accessToken,jwtToken:R.token,user:R.user});return N;}",
        "async function retry(){const v={business:{token:'synthetic-business'}};if(n.wasRejected&&n.wasRejected(v.business)){n.invalidate(v.business)}}",
        "function ttt(){};ttt();NPc();async function NPc(){if(process.argv.includes('-p'))process.stdout.write('fixture-prompt\\n');else process.stdout.write('fixture-version\\n');}",
    ])


def candidate_fixture_run() -> dict:
    with tempfile.TemporaryDirectory(prefix="zcode-auth-acceptance-") as directory:
        home = Path(directory)
        launcher_dir = home / ".local" / "share" / "zcode-worker" / "auth"
        bundle_dir = home / ".local" / "share" / "zcode-cli" / "glm"
        helper_dir = home / ".local" / "bin"
        launcher_dir.mkdir(parents=True)
        bundle_dir.mkdir(parents=True)
        helper_dir.mkdir(parents=True)
        shutil.copy2(HELPER, launcher_dir / "zai-session.cjs")
        shutil.copy2(LAUNCHER, launcher_dir / "launch.cjs")
        (bundle_dir / "zcode.cjs").write_text(fixture_bundle(), encoding="utf-8")
        admission = helper_dir / "zcode_headless_admit.py"
        admission.write_text("#!/usr/bin/env python3\nimport os, sys\nos.execvp(sys.argv[2], sys.argv[2:])\n", encoding="utf-8")
        admission.chmod(0o700)
        # O candidato real sincroniza /opt quando presente; a cópia de fixture
        # desliga só essa origem externa para manter o bundle sintético seguro.
        candidate_fixture = home / "zcode-cli-auth-candidate"
        candidate_fixture.write_text(
            CANDIDATE.read_text(encoding="utf-8").replace(
                'ZCODE_CLI_SRC="/opt/ZCode/resources/glm/zcode.cjs"',
                'ZCODE_CLI_SRC="$HOME/nonexistent-fixture-source.cjs"',
            ),
            encoding="utf-8",
        )
        candidate_fixture.chmod(0o700)
        return run([str(candidate_fixture), "-p", "synthetic prompt"], env={"HOME": str(home)}, timeout=30)


def run_acceptance() -> int:
    if not REAL_BUNDLE.exists():
        return write("acceptance", {"category": "acceptance", "status": "BLOCKED", "reason": "bundle real ausente", "evidence": []})
    real = run(["node", str(LAUNCHER), str(REAL_BUNDLE), "--version"], timeout=60)
    candidate = candidate_fixture_run()
    real_ok = real.get("returncode") == 0 and bool(re.search(r"\d+\.\d+", real.get("stdout", "")))
    candidate_ok = candidate.get("returncode") == 0 and "fixture-prompt" in candidate.get("stdout", "")
    return write("acceptance", {"category": "acceptance", "status": "PASS" if real_ok and candidate_ok else "FAIL",
        "criteria": {"bundle_real_subprocess_version": real_ok, "candidate_prompt_admission_fixture": candidate_ok},
        "commands": {"real_bundle": real, "candidate_fixture": candidate},
        "evidence": [str(LAUNCHER), str(CANDIDATE), str(REAL_BUNDLE)]})


def run_qa() -> int:
    return command_report("qa", [node_tests(), run([sys.executable, "-m", "pytest", "-q", str(PY_TEST)])])


def run_e2e() -> int:
    acceptance = run_acceptance()
    report = json.loads((OUT / "acceptance.json").read_text(encoding="utf-8")) if (OUT / "acceptance.json").exists() else {}
    return write("e2e", {"category": "e2e", "status": "PASS" if acceptance == 0 and report.get("status") == "PASS" else "FAIL",
        "offline": True, "command": report.get("commands", {}), "evidence": [str(NODE_TEST), str(LAUNCHER), str(CANDIDATE)]})


def report_pass_rate(report: dict) -> float | None:
    if isinstance(report.get("pass_rate_pct"), (int, float)):
        return float(report["pass_rate_pct"])
    commands = report.get("commands")
    if isinstance(commands, list) and commands:
        return round(100 * sum(item.get("returncode") == 0 for item in commands) / len(commands), 2)
    return None


def run_metrics() -> int:
    # Executa as fontes ausentes para que métricas não dependam da ordem do gate.
    for category in ("unit", "gherkin", "quality", "mutation", "coverage", "acceptance", "qa", "e2e"):
        if not (OUT / f"{category}.json").exists():
            RUNNERS[category]()
    reports = {}
    for category in CATEGORIES:
        path = OUT / f"{category}.json"
        if path.exists():
            reports[category] = json.loads(path.read_text(encoding="utf-8"))
    coverage = reports.get("coverage", {}).get("metrics", {})
    mutation = reports.get("mutation", {}).get("metrics", {})
    metrics = {"unit_pass_rate": report_pass_rate(reports.get("unit", {})),
        "gherkin_pass_rate": reports.get("gherkin", {}).get("pass_rate_pct"),
        "acceptance_pass_rate": 100.0 if reports.get("acceptance", {}).get("status") == "PASS" else 0.0,
        "qa_pass_rate": report_pass_rate(reports.get("qa", {})),
        "e2e_pass_rate": 100.0 if reports.get("e2e", {}).get("status") == "PASS" else 0.0,
        "coverage_line_pct": coverage.get("coverage_line_pct"), "coverage_branch_pct": coverage.get("coverage_branch_pct"),
        "mutation_score_pct": mutation.get("mutation_score_pct")}
    complete = all(isinstance(value, (int, float)) for value in metrics.values())
    status = "PASS" if complete and metrics["coverage_line_pct"] >= 85 and metrics["coverage_branch_pct"] >= 75 and metrics["mutation_score_pct"] >= 80 else "FAIL"
    return write("metrics", {"category": "metrics", "status": status, "metrics": metrics,
        "scope": {"source": "out/auth/*.json", "auth_sources": ["auth/*.cjs"], "tests": [str(NODE_TEST), str(PY_TEST)]},
        "reports": sorted(reports), "evidence": [str(OUT / f"{category}.json") for category in reports]})


RUNNERS = {name: globals()[f"run_{name}"] for name in CATEGORIES}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("categories", nargs="*", help="categorias a executar; vazio executa todas")
    args = parser.parse_args(argv)
    unknown = [category for category in args.categories if category not in CATEGORIES]
    if unknown:
        parser.error(f"categoria inválida: {', '.join(unknown)}")
    selected = args.categories or list(CATEGORIES)
    statuses = [RUNNERS[category]() for category in selected]
    return 0 if all(status == 0 for status in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
