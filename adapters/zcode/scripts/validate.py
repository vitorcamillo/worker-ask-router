#!/usr/bin/env python3
"""Executa a validação pública da cápsula sem shell e sem alterar ``src``.

Cada categoria é independente e grava evidência em ``out/``. O script não
transforma saída de teste em comando: toda execução usa uma lista argv, ambiente
controlado e prazo finito.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
TEST_ENV = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": "src"}
TIMEOUT = 180.0
THRESHOLDS = {"coverage_line_pct": 100.0, "coverage_branch_pct": 100.0, "mutation_score_pct": 80.0}


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    value = dict(os.environ)
    value.update(TEST_ENV)
    if extra:
        value.update(extra)
    return value


def _run(argv: list[str], *, env: dict[str, str] | None = None, timeout: float = TIMEOUT) -> dict[str, Any]:
    started = time.monotonic()
    record: dict[str, Any] = {"argv": argv}
    try:
        completed = subprocess.run(
            argv,
            cwd=ROOT,
            env=_env(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        record.update(returncode=None, error=type(error).__name__)
    else:
        record.update(
            returncode=completed.returncode,
            stdout=completed.stdout[-4000:],
            stderr=completed.stderr[-4000:],
        )
    record["duration_s"] = round(time.monotonic() - started, 3)
    return record


def _write(name: str, value: Any) -> Path:
    OUT.mkdir(mode=0o700, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _pytest(*args: str) -> list[str]:
    return [sys.executable, "-m", "pytest", *args]


def _all_python() -> list[Path]:
    return sorted([*ROOT.glob("src/**/*.py"), *ROOT.glob("scripts/**/*.py")])


def run_unit() -> int:
    result = _run(_pytest("tests", "-q"))
    _write("unit.json", result)
    return 0 if result.get("returncode") == 0 else 1


def _gherkin_bindings(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    scenarios: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    pending_tests: list[tuple[int, str]] = []

    def finish(item: dict[str, Any] | None) -> None:
        if item is not None:
            item.pop("_binding_candidates", None)
            scenarios.append(item)

    def attach(item: dict[str, Any], candidates: list[tuple[int, str]]) -> None:
        item["bindingCandidates"] = [target for _line, target in candidates]
        if len(candidates) == 1:
            item["test"] = candidates[0][1]
            item["bindingLine"] = candidates[0][0]
        elif len(candidates) > 1:
            item["test"] = None
            item["errors"].append("duplicate test binding")
        else:
            item["test"] = None
            item["bindingLine"] = None

    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        match = re.fullmatch(r"#\s*test:\s*(tests/[^\s]+::test_[A-Za-z0-9_]+)\s*", stripped)
        if match:
            # Do not attach here: this comment may be the binding preceding
            # the next Scenario. Attaching to ``current`` caused an offset
            # and duplicated the last binding in the original parser.
            candidate = (number, match.group(1))
            if current is not None and not current["steps"]:
                # A comment between the Scenario header and its first step is
                # an unambiguous posterior binding for the current scenario.
                if current.get("test") is None:
                    attach(current, [candidate])
                else:
                    current["bindingCandidates"].append(candidate[1])
                    current["errors"].append("duplicate test binding")
            else:
                pending_tests.append(candidate)
            continue
        if stripped.startswith("#") and "test:" in stripped.casefold():
            candidate = (number, "")
            if current is not None and not current["steps"]:
                current["bindingCandidates"].append("")
                current["errors"].append("malformed test binding")
            else:
                pending_tests.append(candidate)
            continue
        if line.lstrip().startswith("Scenario:"):
            finish(current)
            current = {
                "line": number,
                "name": line.split(":", 1)[1].strip(),
                "steps": [],
                "test": None,
                "bindingLine": None,
                "bindingCandidates": [],
                "errors": [],
            }
            if pending_tests:
                attach(current, pending_tests)
                if "" in current["bindingCandidates"]:
                    current["errors"].append("malformed test binding")
                pending_tests = []
        elif current is not None:
            if stripped.startswith(("Given ", "When ", "Then ", "And ")):
                current["steps"].append(stripped.split(" ", 1)[0])
    if pending_tests:
        if current is not None and current.get("test") is None and len(pending_tests) == 1:
            # A binding after the final scenario is an unambiguous posterior
            # binding. Between scenarios, the same syntax is a pending
            # binding for the next Scenario and is handled above.
            attach(current, pending_tests)
            if "" in current["bindingCandidates"]:
                current["errors"].append("malformed test binding")
        elif current is not None:
            current["errors"].append("duplicate test binding")
        else:
            scenarios.append(
                {
                    "line": pending_tests[0][0],
                    "name": None,
                    "steps": [],
                    "test": None,
                    "bindingLine": pending_tests[0][0],
                    "bindingCandidates": [target for _line, target in pending_tests],
                    "errors": ["dangling test binding"],
                }
            )
    finish(current)

    by_test: dict[str, list[dict[str, Any]]] = {}
    for item in scenarios:
        target = item.get("test")
        if target:
            by_test.setdefault(target, []).append(item)
    for target, items in by_test.items():
        if len(items) > 1:
            for item in items:
                item["errors"].append(f"duplicate test binding: {target}")
    for item in scenarios:
        if item.get("name") is not None and not item.get("test"):
            item["errors"].append("missing test binding")
    return scenarios


def run_gherkin() -> int:
    feature = ROOT / "tests" / "cenarios.feature"
    scenarios = _gherkin_bindings(feature)
    valid = len(scenarios) >= 6 and all(
        item.get("name") is not None
        and item.get("test")
        and not item.get("errors")
        and {"Given", "When", "Then"}.issubset(item.get("steps", []))
        for item in scenarios
    )
    executions = []
    if valid:
        for item in scenarios:
            executions.append({"scenario": item, "result": _run(_pytest(item["test"], "-q"))})
    report = {"feature": str(feature.relative_to(ROOT)), "scenarios": scenarios, "executions": executions}
    _write("gherkin.json", report)
    return 0 if valid and all(item["result"].get("returncode") == 0 for item in executions) else 1


def _quality_findings() -> list[str]:
    findings: list[str] = []
    for path in _all_python():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as error:
            findings.append(f"{path}: {type(error).__name__}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
                findings.append(f"{path}:{node.lineno}: execução dinâmica")
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                        findings.append(f"{path}:{node.lineno}: shell=True")
    return findings


def run_quality() -> int:
    compile_result = _run([sys.executable, "-m", "compileall", "-q", "src", "scripts"])
    ruff_result = _run(["ruff", "check", "--select", "F,E9", "src", "scripts"])
    findings = _quality_findings()
    report = {"compile": compile_result, "ruff": ruff_result, "findings": findings, "docs": bool((ROOT / "docs" / "README.md").exists())}
    _write("quality.json", report)
    return 0 if compile_result.get("returncode") == 0 and ruff_result.get("returncode") == 0 and not findings and report["docs"] else 1


def _complexity(tree: ast.AST) -> int:
    branches = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.IfExp, ast.Match)
    return 1 + sum(isinstance(node, branches) for node in ast.walk(tree))


def run_metrics() -> int:
    modules = []
    for path in _all_python():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        modules.append(
            {
                "module": str(path.relative_to(ROOT)),
                "lines": len(source.splitlines()),
                "functions": len(functions),
                "complexity": _complexity(tree),
            }
        )
    report = {"scope": "src/ e scripts/", "limits": {"module_lines": 1200, "complexity": 250}, "modules": modules}
    _write("metrics.json", report)
    return 0 if all(item["lines"] <= 1200 and item["complexity"] <= 250 for item in modules) else 1


def _coverage_xml(coverage: dict[str, Any]) -> dict[str, float]:
    totals = coverage.get("totals", {})
    statements = int(totals.get("num_statements", 0))
    covered = int(totals.get("covered_lines", 0))
    branches = int(totals.get("num_branches", 0))
    covered_branches = int(totals.get("covered_branches", 0))
    line_pct = 100.0 * covered / statements if statements else 0.0
    branch_pct = 100.0 * covered_branches / branches if branches else 0.0
    root = ET.Element("report", name="python")
    ET.SubElement(root, "counter", type="LINE", missed=str(statements - covered), covered=str(covered))
    ET.SubElement(root, "counter", type="BRANCH", missed=str(branches - covered_branches), covered=str(covered_branches))
    xml_bytes = ET.tostring(root, encoding="utf-8")
    for path in (ROOT / "target" / "site" / "jacoco" / "jacoco.xml", OUT / "target" / "site" / "jacoco" / "jacoco.xml"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(xml_bytes)
    return {"coverage_line_pct": line_pct, "coverage_branch_pct": branch_pct}


def run_coverage() -> int:
    OUT.mkdir(mode=0o700, exist_ok=True)
    metrics: dict[str, float] = {"coverage_line_pct": 0.0, "coverage_branch_pct": 0.0}
    with tempfile.TemporaryDirectory(prefix="zcode-coverage-") as directory:
        data_file = Path(directory) / ".coverage"
        json_file = Path(directory) / "coverage.json"
        coverage_env = {"COVERAGE_FILE": str(data_file)}
        run = _run(
            ["coverage", "run", "--branch", "--source=src/zcode_worker", "--omit=*/base.py", "-m", "pytest", "tests", "-q"],
            env=coverage_env,
        )
        json_run = _run(["coverage", "json", "-o", str(json_file)], env=coverage_env)
        try:
            coverage_data = json.loads(json_file.read_text(encoding="utf-8"))
            # The adapter consumes coverage's real counters; it never derives
            # percentages from a stale XML or rounds them upward.
            metrics = _coverage_xml(coverage_data)
            (OUT / "coverage.json").write_text(
                json.dumps(coverage_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except (OSError, json.JSONDecodeError, ET.ParseError):
            pass
    report = {"run": run, "json": json_run, **metrics, "thresholds": THRESHOLDS}
    _write("coverage-report.json", report)
    return 0 if run.get("returncode") == 0 and json_run.get("returncode") == 0 and all(metrics[key] >= value for key, value in THRESHOLDS.items() if key != "mutation_score_pct") else 1


def _mutants() -> list[dict[str, str]]:
    return [
        {
            "name": "effective-selection-payload-bypass",
            "file": "src/zcode_worker/features.py",
            "old": "actual = effective_selection(session_id, db_path=db_path) if session_id else None",
            "new": "actual = payload.get(\"effective\") if session_id else None",
            "test": "tests/test_validation.py::test_effective_selection_uses_database_not_claimed_payload",
        },
        {
            "name": "projection-error-guard",
            "file": "src/zcode_worker/features.py",
            "old": "        elif isinstance(projection, str):\n            projection_status = projection\n        else:\n            raise FeatureError(\"projection inválida\")",
            "new": "        elif isinstance(projection, str):\n            projection_status = projection\n        else:\n            projection_status = None",
            "test": "tests/test_validation.py::test_projection_must_be_string_or_object",
        },
        {
            "name": "retry-deadline-boundary",
            "file": "src/zcode_worker/cli.py",
            "old": "    if time.monotonic() + delay >= deadline:\n        raise features.FeatureError(\"prazo esgotado durante retry\", \"timeout\")",
            "new": "    if time.monotonic() + delay > deadline:\n        raise features.FeatureError(\"prazo esgotado durante retry\", \"timeout\")",
            "test": "tests/test_validation.py::test_retry_deadline_is_inclusive",
        },
        {
            "name": "output-limit-boundary",
            "file": "src/zcode_worker/cli.py",
            "old": "                if total > max_output_bytes:\n                    raise OutputLimitExceeded()",
            "new": "                if total >= max_output_bytes:\n                    raise OutputLimitExceeded()",
            "test": "tests/test_validation.py::test_output_limit_allows_exact_boundary",
        },
        {
            "name": "attachment-limit-inclusive",
            "file": "src/zcode_worker/features.py",
            "old": "    if len(paths) > MAX_ATTACHMENTS:",
            "new": "    if len(paths) >= MAX_ATTACHMENTS:",
            "test": "tests/test_validation.py::test_five_attachments_are_allowed",
        },
        {
            "name": "identity-requires-pid-and-job-marker",
            "file": "src/zcode_worker/jobs.py",
            "old": "    return b\"--_job-child\" in argv and job_id.encode(\"ascii\") in argv",
            "new": "    return b\"--_job-child\" in argv or job_id.encode(\"ascii\") in argv",
            "test": "tests/test_validation.py::test_process_identity_requires_job_marker_and_id",
        },
        {
            "name": "turn-request-exit-code",
            "file": "src/zcode_worker/cli.py",
            "old": "exit_code = 2 if pending is not None else 0",
            "new": "exit_code = 0 if pending is not None else 0",
            "test": "tests/test_conversation.py::test_run_task_reports_need_input_with_exit_code_2",
        },
        {
            "name": "turn-request-pending-payload",
            "file": "src/zcode_worker/cli.py",
            "old": "result[\"pending\"] = pending",
            "new": "pass",
            "test": "tests/test_conversation.py::test_run_task_reports_need_input_with_exit_code_2",
        },
        {
            "name": "turn-end-event-status",
            "file": "src/zcode_worker/cli.py",
            "old": "_emit(event_sink, {\"kind\": \"turn_end\", \"status\": status, \"exitCode\": exit_code})",
            "new": "_emit(event_sink, {\"kind\": \"turn_end\", \"status\": \"ok\", \"exitCode\": exit_code})",
            "test": "tests/test_conversation.py::test_run_child_records_error_and_need_input_events",
        },
        {
            "name": "cancel-forced-flag",
            "file": "src/zcode_worker/jobs.py",
            "old": "result[\"killForced\"] = True",
            "new": "pass",
            "test": "tests/test_conversation.py::test_cancel_two_stage_forces_kill_on_stubborn_process",
        },
        {
            "name": "events-truncation-marker",
            "file": "src/zcode_worker/jobs.py",
            "old": "if b'\"events_truncated\"' not in tail:",
            "new": "if False:",
            "test": "tests/test_conversation.py::test_append_event_validates_shape_and_limits",
        },
        {
            "name": "bridge-ask-deadline",
            "file": "src/zcode_worker/bridge.py",
            "old": "raise BridgeError(\"orquestrador não respondeu no prazo\")",
            "new": "return \"prazo ignorado\"",
            "test": "tests/test_conversation.py::test_bridge_ask_orchestrator_timeout_inprocess",
        },
        {
            "name": "bridge-ask-is-persisted",
            "file": "src/zcode_worker/bridge.py",
            "old": "jobs._append_event(os.path.join(directory, \"asks.jsonl\"), ask)",
            "new": "pass",
            "test": "tests/test_conversation.py::test_bridge_ask_orchestrator_flow_inprocess",
        },
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_module(mutant: dict[str, str]) -> str:
    return "zcode_worker." + Path(mutant["file"]).stem


def _junit_summary(path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {"exists": path.exists(), "failures": [], "errors": []}
    if not path.exists():
        return summary
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        summary["parse_error"] = type(error).__name__
        return summary
    for testcase in root.iter("testcase"):
        identity = f"{testcase.attrib.get('classname', '')}::{testcase.attrib.get('name', '')}"
        for failure in testcase.findall("failure"):
            summary["failures"].append(
                {"test": identity, "type": failure.attrib.get("type"), "text": (failure.text or "")[-2000:]}
            )
        for error in testcase.findall("error"):
            summary["errors"].append(
                {"test": identity, "type": error.attrib.get("type"), "text": (error.text or "")[-2000:]}
            )
    return summary


def _assertion_failure(junit: dict[str, Any]) -> bool:
    for failure in junit.get("failures", []):
        failure_type = str(failure.get("type") or "")
        text = str(failure.get("text") or "")
        if failure_type.endswith("AssertionError") or "AssertionError" in text or re.search(r"\bE\s+assert\b", text):
            return True
    return False


def _classify_mutant(result: dict[str, Any], junit: dict[str, Any]) -> tuple[str, str]:
    returncode = result.get("returncode")
    if returncode == 0:
        return "SURVIVED", "pytest passou sem detectar a mutação"
    if returncode == 1 and _assertion_failure(junit):
        return "KILLED", "falha de asserção confirmada no JUnit XML"
    if junit.get("errors") or "error collecting" in str(result.get("stdout", "")).lower():
        return "COLLECTION_ERROR", "pytest reportou erro de coleta"
    if result.get("error") == "TimeoutExpired":
        return "TIMEOUT", "pytest excedeu o prazo"
    return "GATE_FAIL", "resultado não é uma falha de asserção classificável"


def _mutant_result(mutant: dict[str, str], source_root: Path) -> dict[str, Any]:
    path = source_root / mutant["file"]
    text = path.read_text(encoding="utf-8")
    result: dict[str, Any] = {
        "name": mutant["name"],
        "file": mutant["file"],
        "old": mutant["old"],
        "new": mutant["new"],
        "test": mutant["test"],
        "source_hash_before": _sha256(path),
        "mutation_saved": False,
    }
    if text.count(mutant["old"]) != 1:
        result.update(status="COLLECTION_ERROR", reason="trecho ausente ou não é único")
        return result
    path.write_text(text.replace(mutant["old"], mutant["new"], 1), encoding="utf-8")
    result["mutation_saved"] = True
    result["source_hash_after"] = _sha256(path)
    env = {"PYTHONPATH": str(source_root / "src")}
    probe = _run(
        [sys.executable, "-c", "import importlib; print(importlib.import_module(__import__('sys').argv[1]).__file__)", _source_module(mutant)],
        env=env,
    )
    imported = (probe.get("stdout") or "").strip().splitlines()
    imported_file = imported[-1] if imported else None
    expected_root = (source_root / "src").resolve()
    imported_path = Path(imported_file).resolve() if imported_file else None
    source_verified = bool(
        imported_path is not None
        and os.path.commonpath((str(expected_root), str(imported_path))) == str(expected_root)
    )
    result.update(
        import_probe=probe,
        imported_file=imported_file,
        source_root_verified=source_verified,
    )
    if not source_verified:
        result.update(status="GATE_FAIL", reason="subprocess importou source fora da cópia mutante")
        return result
    junit_path = source_root / f"{mutant['name']}.junit.xml"
    test_result = _run(_pytest(mutant["test"], "-q", f"--junitxml={junit_path}"), env=env)
    junit = _junit_summary(junit_path)
    status, reason = _classify_mutant(test_result, junit)
    result.update(test_result, junit=junit, status=status, reason=reason)
    return result


def run_mutation() -> int:
    baseline_tests = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "tests").glob("test_*.py")
    )
    baseline = _run(_pytest("tests", "-q"))
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="zcode-mutants-") as directory:
        source_root = Path(directory)
        for mutant in _mutants():
            individual_baseline = _run(_pytest(mutant["test"], "-q"))
            mutant_root = source_root / mutant["name"]
            shutil.copytree(
                ROOT / "src",
                mutant_root / "src",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            item = _mutant_result(mutant, mutant_root)
            item["individual_baseline"] = individual_baseline
            if baseline.get("returncode") != 0 or individual_baseline.get("returncode") != 0:
                item["status"] = "GATE_FAIL"
                item["reason"] = "baseline da suíte ou do teste individual falhou"
            results.append(item)
    killed = sum(item.get("status") == "KILLED" for item in results)
    score = 100.0 * killed / len(results) if results else 0.0
    root = ET.Element("mutations")
    for item in results:
        ET.SubElement(root, "mutation", detected=str(item.get("status") == "KILLED").lower(), status=item.get("status", "UNKNOWN"), name=item.get("name", "unknown"))
    xml = ET.tostring(root, encoding="utf-8")
    for path in (ROOT / "target" / "pit-reports" / "mutations.xml", OUT / "target" / "pit-reports" / "mutations.xml"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(xml)
    gate_failures = sum(item.get("status") not in {"KILLED", "SURVIVED"} for item in results)
    report = {
        "baseline": baseline,
        "baseline_tests": baseline_tests,
        "mutants": results,
        "killed": killed,
        "survived": sum(item.get("status") == "SURVIVED" for item in results),
        "gate_failures": gate_failures,
        "total": len(results),
        "mutation_score_pct": score,
        "threshold": THRESHOLDS["mutation_score_pct"],
    }
    _write("mutations.json", report)
    return 0 if baseline.get("returncode") == 0 and results and not gate_failures and score >= THRESHOLDS["mutation_score_pct"] else 1


def _run_mark(mark: str, name: str) -> int:
    result = _run(_pytest("tests/test_gate_extra.py", "-m", mark, "-q"))
    _write(name, result)
    return 0 if result.get("returncode") == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="validação pública do worker")
    parser.add_argument("category", choices=("unit", "gherkin", "quality", "metrics", "coverage", "mutation", "acceptance", "qa", "e2e"))
    category = parser.parse_args(argv).category
    runners = {
        "unit": run_unit,
        "gherkin": run_gherkin,
        "quality": run_quality,
        "metrics": run_metrics,
        "coverage": run_coverage,
        "mutation": run_mutation,
        "acceptance": lambda: _run_mark("acceptance", "acceptance.json"),
        "qa": lambda: _run_mark("qa", "qa.json"),
        "e2e": lambda: _run_mark("e2e", "e2e.json"),
    }
    code = runners[category]()
    print(f"VALIDATION {category}: {'PASS' if code == 0 else 'FAIL'}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
