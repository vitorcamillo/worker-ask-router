"""Testes do validador: bindings, evidência e classificação sem métricas fabricadas."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from scripts import validate
from zcode_worker import cli, features, jobs


ROOT = Path(__file__).resolve().parents[1]


def _feature(*body: str) -> str:
    return "\n".join(("Feature: teste", *body)) + "\n"


def test_real_gherkin_bindings_are_one_to_one_and_all_steps_are_valid():
    scenarios = validate._gherkin_bindings(ROOT / "tests" / "cenarios.feature")

    assert len(scenarios) == 6
    assert all(not item["errors"] for item in scenarios)
    assert all({"Given", "When", "Then"}.issubset(item["steps"]) for item in scenarios)
    assert [item["test"] for item in scenarios] == [
        "tests/test_gate_extra.py::test_cli_foreground_stdin_json",
        "tests/test_gate_extra.py::test_cli_rejects_effective_selection_mismatch",
        "tests/test_gate_extra.py::test_cli_dry_run_doctor_and_argument_policy",
        "tests/test_gate_extra.py::test_cli_timeout_and_output_limit_are_safe",
        "tests/test_gate_extra.py::test_adversarial_attachments_denylist_and_json_are_rejected",
        "tests/test_gate_extra.py::test_private_background_job_status_cancel_and_cleanup",
    ]


def test_gherkin_parser_reports_missing_dangling_duplicate_and_invalid_steps(tmp_path):
    missing = tmp_path / "missing.feature"
    missing.write_text(_feature("  Scenario: sem binding", "    Given algo", "    When algo", "    Then algo"), encoding="utf-8")
    missing_items = validate._gherkin_bindings(missing)
    assert "missing test binding" in missing_items[0]["errors"]

    dangling = tmp_path / "dangling.feature"
    dangling.write_text(_feature("  # test: tests/test_validation.py::test_x"), encoding="utf-8")
    dangling_items = validate._gherkin_bindings(dangling)
    assert any("dangling test binding" in item["errors"] for item in dangling_items)

    duplicate = tmp_path / "duplicate.feature"
    duplicate.write_text(
        _feature(
            "  # test: tests/test_validation.py::test_x",
            "  # test: tests/test_validation.py::test_y",
            "  Scenario: duplicado",
            "    Given algo",
            "    When algo",
            "    Then algo",
        ),
        encoding="utf-8",
    )
    duplicate_items = validate._gherkin_bindings(duplicate)
    assert "duplicate test binding" in duplicate_items[0]["errors"]

    invalid_steps = tmp_path / "steps.feature"
    invalid_steps.write_text(
        _feature(
            "  # test: tests/test_validation.py::test_x",
            "  Scenario: sem Then",
            "    Given algo",
            "    When algo",
            "    And algo",
        ),
        encoding="utf-8",
    )
    invalid_item = validate._gherkin_bindings(invalid_steps)[0]
    assert not {"Given", "When", "Then"}.issubset(invalid_item["steps"])


def test_gherkin_posterior_binding_is_assigned_once(tmp_path):
    feature = tmp_path / "posterior.feature"
    feature.write_text(
        _feature(
            "  Scenario: posterior",
            "    Given algo",
            "    When algo",
            "    Then algo",
            "  # test: tests/test_validation.py::test_x",
        ),
        encoding="utf-8",
    )

    item = validate._gherkin_bindings(feature)[0]
    assert item["test"] == "tests/test_validation.py::test_x"
    assert item["bindingCandidates"] == [item["test"]]
    assert not item["errors"]


def test_gherkin_header_posterior_binding_is_not_shifted(tmp_path):
    feature = tmp_path / "header-posterior.feature"
    feature.write_text(
        _feature(
            "  Scenario: posterior no cabeçalho",
            "  # test: tests/test_validation.py::test_x",
            "    Given algo",
            "    When algo",
            "    Then algo",
        ),
        encoding="utf-8",
    )

    item = validate._gherkin_bindings(feature)[0]
    assert item["test"] == "tests/test_validation.py::test_x"
    assert not item["errors"]


def test_quality_uses_real_boolean_for_missing_docs_and_covers_candidate(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "scripts" / "make_candidate.py").write_text("value = 2\n", encoding="utf-8")
    out = tmp_path / "out"
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return {"argv": argv, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(validate, "ROOT", tmp_path)
    monkeypatch.setattr(validate, "OUT", out)
    monkeypatch.setattr(validate, "_run", fake_run)

    assert validate.run_quality() == 1
    report = json.loads((out / "quality.json").read_text(encoding="utf-8"))
    assert report["docs"] is False
    assert any(argv[-2:] == ["src", "scripts"] for argv in calls)
    assert "scripts/make_candidate.py" in {
        str(path.relative_to(tmp_path)) for path in validate._all_python()
    }


def test_mutation_classification_requires_real_assertion_failure(tmp_path):
    assertion_xml = tmp_path / "assertion.xml"
    assertion_xml.write_text(
        '<testsuite><testcase classname="x" name="y"><failure type="AssertionError">E assert 1 == 2</failure></testcase></testsuite>',
        encoding="utf-8",
    )
    assertion = validate._junit_summary(assertion_xml)
    assert validate._classify_mutant({"returncode": 1}, assertion)[0] == "KILLED"
    assert validate._classify_mutant({"returncode": 0}, {"failures": [], "errors": []})[0] == "SURVIVED"

    runtime_xml = tmp_path / "runtime.xml"
    runtime_xml.write_text(
        '<testsuite><testcase classname="x" name="y"><failure type="RuntimeError">boom</failure></testcase></testsuite>',
        encoding="utf-8",
    )
    runtime = validate._junit_summary(runtime_xml)
    assert validate._classify_mutant({"returncode": 1}, runtime)[0] == "GATE_FAIL"

    collection_xml = tmp_path / "collection.xml"
    collection_xml.write_text(
        '<testsuite><testcase classname="x" name="y"><error type="ImportError">cannot import</error></testcase></testsuite>',
        encoding="utf-8",
    )
    collection = validate._junit_summary(collection_xml)
    assert validate._classify_mutant({"returncode": 2}, collection)[0] == "COLLECTION_ERROR"
    assert validate._classify_mutant({"returncode": None, "error": "TimeoutExpired"}, {"failures": [], "errors": []})[0] == "TIMEOUT"


def test_missing_or_non_unique_mutant_source_is_collection_error(tmp_path):
    source = tmp_path / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    mutant = {
        "name": "missing",
        "file": "src/module.py",
        "old": "not present",
        "new": "value = 2",
        "test": "tests/test_validation.py::test_x",
    }
    result = validate._mutant_result(mutant, tmp_path)
    assert result["status"] == "COLLECTION_ERROR"
    assert result["mutation_saved"] is False


def test_coverage_adapter_preserves_real_line_and_branch_counters(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setattr(validate, "ROOT", tmp_path)
    monkeypatch.setattr(validate, "OUT", out)
    metrics = validate._coverage_xml(
        {"totals": {"num_statements": 10, "covered_lines": 8, "num_branches": 4, "covered_branches": 2}}
    )

    assert metrics == {"coverage_line_pct": 80.0, "coverage_branch_pct": 50.0}
    xml = (tmp_path / "target" / "site" / "jacoco" / "jacoco.xml").read_text(encoding="utf-8")
    assert 'type="LINE" missed="2" covered="8"' in xml
    assert 'type="BRANCH" missed="2" covered="2"' in xml


def test_effective_selection_uses_database_not_claimed_payload(tmp_path):
    expected = features.requested_selection("glm-5.3-flash", "high")
    rejected = False
    try:
        features.verify_effective(
            {"sessionId": "sess_claimed", "effective": expected},
            "glm-5.3-flash",
            "high",
            db_path=tmp_path / "missing.sqlite",
        )
    except features.FeatureError:
        rejected = True
    assert rejected


def test_projection_must_be_string_or_object():
    rejected = False
    try:
        features.validate_response({"sessionId": "sess_projection", "response": "ok", "projection": []})
    except features.FeatureError:
        rejected = True
    assert rejected


def test_retry_deadline_is_inclusive(monkeypatch):
    monkeypatch.setattr(cli.base, "RETRY_DELAYS", (1.0,))
    monkeypatch.setattr(cli.time, "monotonic", lambda: 100.0)
    slept = []
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    error = features.FeatureError("rate", "rate_limit")

    raised = None
    try:
        cli._retry_allowed(SimpleNamespace(mode="plan"), error, 0, 101.0)
    except features.FeatureError as caught:
        raised = caught
    assert raised is not None
    assert raised.code == "timeout"
    assert slept == []


def test_output_limit_allows_exact_boundary(tmp_path):
    command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'abcd')"]
    result = None
    try:
        result = cli._invoke_limited(command, dict(os.environ), time.monotonic() + 5.0, 4)
    except features.FeatureError:
        result = (None, None, None)
    assert result[0] == 0
    assert result[1] == "abcd"


def test_five_attachments_are_allowed(tmp_path):
    paths = []
    for index in range(5):
        path = tmp_path / f"note-{index}.txt"
        path.write_text("ok", encoding="utf-8")
        paths.append(str(path))
    result = []
    try:
        result = features.validate_attachments(paths)
    except features.FeatureError:
        result = []
    assert len(result) == 5


def test_process_identity_requires_job_marker_and_id(tmp_path, monkeypatch):
    pid = 4242
    proc = tmp_path / str(pid)
    proc.mkdir()
    (proc / "cmdline").write_bytes(b"python\0other\0job-4242\0")
    monkeypatch.setattr(jobs, "_proc_details", lambda _pid: (os.getuid(), 7, pid, pid))
    monkeypatch.setattr(jobs, "_PROC_ROOT", str(tmp_path))

    assert jobs._proc_has_identity(pid, os.getuid(), 7, "job-4242") is False
