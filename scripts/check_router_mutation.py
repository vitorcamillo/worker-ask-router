"""Mutação direcionada de invariantes; amostra explícita, nunca score global."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parents[1] / "out"
ROOT = HERE.parent
SOURCE = ROOT / "worker_router.py"
TESTS = ROOT / "tests/test_worker_router.py"
# Each mutation changes one unique production rule and runs behavioral tests.
MUTATIONS = [
    ("quota-only", "if rc == 75:", "if rc == 74:", "75_then_fallback"),
    ("partial-write", "if snap_after != snap_before:", "if False:", "partial_write_blocks"),
    ("cooldown", "if not args.retry_exhausted:", "if False:", "cooldown_skips"),
    ("muse-opt-out", "if allow_muse:", "if True:", "no_muse"),
    ("no-recursion", 'if os.environ.get("WORKER_ASK_ACTIVE") == "1":', "if False:", "delegate_recursively"),
    ("full-chain", "if allow_muse:\n        chain.append", "if False:\n        chain.append", "muse_in_chain_by_default"),
    ("write-scope", 'if args.mode == "write" and not allowed:', "if False:", "invalid_cli_inputs"),
    ("finite-time", "if not math.isfinite(value):", "if False:", "timestamp_is_finite"),
    ("bounded-time", "if value < 0 or value > now + MAX_RESET_SECONDS + 1:", "if False:", "timestamp_is_finite"),
    ("readonly-zcode", 'm = "edit" if mode == "write" else "plan"', 'm = "edit"', "argv_modes_are_explicit"),
    ("readonly-codex", 'm = "write" if mode == "write" else "plan"\n        return [path, "--mode", m, "--cwd", cwd_abs]', 'm = "write"\n        return [path, "--mode", m, "--cwd", cwd_abs]', "argv_modes_are_explicit"),
    ("cwd", "cwd_abs = os.path.abspath(cwd)", 'cwd_abs = "."', "argv_modes_are_explicit"),
    ("control-char", "if ord(ch) < 32 or ord(ch) == 127:", "if False:", "unsafe_allowed_paths"),
    ("sensitive-spec", "if _is_sensitive(part):\n                    raise RuntimeError(\"spec sensivel recusada\")", "if False:\n                    raise RuntimeError(\"spec sensivel recusada\")", "sensitive_spec"),
    ("sensitive-scope", 'if low in (".ssh", ".aws", "credentials", "credential"):', "if False:", "unsafe_allowed_paths"),
    ("no-symlink", 'flags | getattr(os, "O_NOFOLLOW", 0)', "flags", "symlink_broken_cache"),
    ("preserve-cooldown", 'entry.get("blocked_reason") != "timeout" and previous["until"] > entry["until"]:', 'entry.get("blocked_reason") != "timeout" and False:', "later_cooldown"),
]


def main():
    source = SOURCE.read_text()
    report = {"scope": "17 targeted mutations of fallback, recursion, scope, privacy, cache and argv; not exhaustive",
              "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
              "tests_sha256": hashlib.sha256(TESTS.read_bytes()).hexdigest(), "results": []}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="worker-router-mutation-") as directory:
        stage = Path(directory)
        (stage / "tests").mkdir()
        shutil.copy2(TESTS, stage / "tests/test_worker_router.py")
        for helper in ("quota_reset.py", "quota_provider.py", "conftest.py"):
            shutil.copy2(ROOT / helper, stage / helper)
        (stage / "pytest.ini").write_text("[pytest]\ntestpaths=tests\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
        def run(selection=None):
            argv = [sys.executable, "-m", "pytest", "tests", "-q"]
            if selection:
                argv += ["-k", selection]
            return subprocess.run(argv, cwd=stage, env=env, capture_output=True, timeout=180)
        (stage / "worker_router.py").write_text(source)
        baseline = run()
        if baseline.returncode:
            print(baseline.stdout.decode(errors="replace")[-2000:])
            return 1
        for name, before, after, selection in MUTATIONS:
            if source.count(before) != 1:
                raise ValueError("mutation no longer uniquely matches: " + name)
            (stage / "worker_router.py").write_text(source.replace(before, after, 1))
            shutil.rmtree(stage / "__pycache__", ignore_errors=True)
            try:
                result = run(selection)
                # pytest exit 1 = assertion failure; collection/infra never counts as killed.
                status = "killed" if result.returncode == 1 else "survived" if result.returncode == 0 else "tool_error"
                code = result.returncode
            except subprocess.TimeoutExpired:
                status, code = "timeout", None
            report["results"].append({"name": name, "status": status, "exit_code": code, "selection": selection})
            print(name, status, flush=True)
    killed = sum(item["status"] == "killed" for item in report["results"])
    report.update(killed=killed, total=len(MUTATIONS), score_pct=100 * killed / len(MUTATIONS),
                  duration_s=round(time.monotonic() - started, 3))
    (HERE / "mutation.json").write_text(json.dumps(report, indent=2) + "\n")
    xml = ET.Element("mutations")
    for item in report["results"]:
        ET.SubElement(xml, "mutation", detected=str(item["status"] == "killed").lower(), status=item["status"].upper(), name=item["name"])
    target = HERE / "target/pit-reports/mutations.xml"
    target.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(xml).write(target, encoding="unicode")
    return 0 if report["score_pct"] >= 80 and all(item["status"] in ("killed", "survived") for item in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
