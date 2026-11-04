#!/usr/bin/env python3
"""Cobertura de funções e mutação da contagem de admissão, sem processos reais."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out-admission"
SOURCES = ("adapters/zcode/src/zcode_worker/base.py", "bin/zcode-worker")
TEST = "adapters/zcode/tests/test_cli_admission.py"
OLD_RETURN = "return sum(parent not in processes or group != processes[parent][1] for parent, group in processes.values())"
MUTATIONS = (
    ("count-helpers", OLD_RETURN, "return len(processes)", "parent_plus_four_helpers"),
    ("merge-independent", OLD_RETURN, "return sum(parent not in processes for parent, group in processes.values())", "independent_group"),
    ("merge-unrelated", OLD_RETURN, "return len({group for parent, group in processes.values()})", "unrelated_roots"),
    ("fail-open", 'except (PermissionError, OSError, UnicodeError, ValueError, IndexError):\n                raise AdmissionError("não foi possível inspecionar processos para admissão") from None',
     'except (PermissionError, OSError, UnicodeError, ValueError, IndexError):\n                continue', "invalid_metadata"),
)


def coverage():
    data = json.loads((OUT / "coverage.json").read_text())
    line_hit = line_total = branch_hit = branch_total = 0
    details = []
    for relative in SOURCES:
        source = ROOT / relative
        node = next(n for n in ast.parse(source.read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == "count_active_cli_processes")
        entry = data["files"][relative]
        executed = {n for n in entry["executed_lines"] if node.lineno <= n <= node.end_lineno}
        missing = {n for n in entry["missing_lines"] if node.lineno <= n <= node.end_lineno}
        good = [v for v in entry["executed_branches"] if node.lineno <= v[0] <= node.end_lineno]
        bad = [v for v in entry["missing_branches"] if node.lineno <= v[0] <= node.end_lineno]
        line_hit += len(executed)
        line_total += len(executed | missing)
        branch_hit += len(good)
        branch_total += len(good) + len(bad)
        details.append({"source": relative, "scope": node.name, "missing_lines": sorted(missing), "missing_branches": bad})
    metrics = {"coverage_line_pct": 100 * line_hit / line_total,
               "coverage_branch_pct": 100 * branch_hit / branch_total}
    (OUT / "coverage-scope.json").write_text(json.dumps({"metrics": metrics, "scope": details}, indent=2))
    xml = ET.Element("report")
    ET.SubElement(xml, "counter", type="LINE", covered=str(line_hit), missed=str(line_total - line_hit))
    ET.SubElement(xml, "counter", type="BRANCH", covered=str(branch_hit), missed=str(branch_total - branch_hit))
    path = OUT / "target/site/jacoco/jacoco.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(xml).write(path, encoding="unicode")
    print(metrics)
    return 0 if metrics["coverage_line_pct"] >= 85 and metrics["coverage_branch_pct"] >= 75 else 1


def mutation():
    files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    results = []
    with tempfile.TemporaryDirectory(prefix="admission-mutation-") as directory:
        stage = Path(directory)
        for relative in files:
            if relative:
                dest = stage / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, dest)
        for relative in SOURCES:
            target = stage / relative
            source = (ROOT / relative).read_text()
            for name, old, new, selection in MUTATIONS:
                assert source.count(old) == 1, (relative, name)
                def run():
                    return subprocess.run([sys.executable, "-m", "pytest", "-q", TEST, "-k", selection],
                                          cwd=stage, env=env, capture_output=True, text=True, timeout=30)
                target.write_text(source)
                shutil.rmtree(target.parent / "__pycache__", ignore_errors=True)
                baseline = run()
                assert baseline.returncode == 0, baseline.stdout + baseline.stderr
                target.write_text(source.replace(old, new, 1))
                shutil.rmtree(target.parent / "__pycache__", ignore_errors=True)
                changed = run()
                killed = changed.returncode == 1 and "FAILED" in changed.stdout and "ERROR collecting" not in changed.stdout
                results.append({"source": relative, "name": name, "killed": killed})
                print(relative, name, "killed" if killed else "survived", flush=True)
            target.write_text(source)
    (OUT / "mutation.json").write_text(json.dumps({"scope": "eight targeted admission mutations; not exhaustive", "results": results}, indent=2))
    xml = ET.Element("mutations")
    for item in results:
        ET.SubElement(xml, "mutation", detected=str(item["killed"]).lower(), status="KILLED" if item["killed"] else "SURVIVED")
    path = OUT / "target/pit-reports/mutations.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(xml).write(path, encoding="unicode")
    return 0 if all(v["killed"] for v in results) else 1


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    if len(sys.argv) != 2 or sys.argv[1] not in ("coverage", "mutation"):
        raise SystemExit("uso: check_admission.py coverage|mutation")
    raise SystemExit(coverage() if sys.argv[1] == "coverage" else mutation())
