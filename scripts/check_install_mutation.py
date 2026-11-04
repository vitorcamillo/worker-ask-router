#!/usr/bin/env python3
"""Amostra explícita de mutação das guardas e do rollback do instalador."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/install.py"
MUTATIONS = (
    ("preflight-directory", "if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):", "if False:", "preflight_rejects_directory"),
    ("hardlink-private", "if stat.S_ISREG(mode) and info.st_nlink != 1:", "if False:", "hardlinked_destination"),
    ("restore-old", "os.replace(saved, dest)", "pass", "failure_restores_previous"),
    ("remove-new", "dest.unlink(missing_ok=True)", "pass", "failed_fresh_install"),
    ("concurrent-lock", "fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)", "pass", "lock_refuses_concurrent"),
)


def main():
    files = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    ).decode().split("\0")
    source = SOURCE.read_text()
    results = []
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    with tempfile.TemporaryDirectory(prefix="install-mutation-") as directory:
        stage = Path(directory)
        for relative in files:
            if not relative:
                continue
            origin = ROOT / relative
            dest = stage / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, dest)
        target = stage / "scripts/install.py"
        for name, old, new, selection in MUTATIONS:
            assert source.count(old) == 1, name
            def run():
                return subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "tests/test_install.py", "-k", selection],
                    cwd=stage, env=env, capture_output=True, text=True, timeout=30,
                )
            target.write_text(source)
            shutil.rmtree(target.parent / "__pycache__", ignore_errors=True)
            baseline = run()
            assert baseline.returncode == 0, baseline.stdout + baseline.stderr
            target.write_text(source.replace(old, new, 1))
            shutil.rmtree(target.parent / "__pycache__", ignore_errors=True)
            mutant = run()
            killed = (mutant.returncode == 1 and "FAILED" in mutant.stdout
                      and "ERROR collecting" not in mutant.stdout)
            results.append({"name": name, "killed": killed, "exit_code": mutant.returncode})
            print(name, "killed" if killed else "survived", flush=True)
    out = ROOT / "out-installer"
    out.mkdir(exist_ok=True)
    report = {"scope": "five targeted installer invariants; not exhaustive",
              "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
              "results": results, "mutation_score_pct": 100 * sum(x["killed"] for x in results) / len(results)}
    (out / "mutation.json").write_text(json.dumps(report, indent=2) + "\n")
    xml = ET.Element("mutations")
    for result in results:
        ET.SubElement(xml, "mutation", name=result["name"], detected=str(result["killed"]).lower(),
                      status="KILLED" if result["killed"] else "SURVIVED")
    path = out / "target/pit-reports/mutations.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(xml).write(path, encoding="unicode")
    return 0 if all(x["killed"] for x in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
