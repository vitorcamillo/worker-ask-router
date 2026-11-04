"""Classificação segura das falhas de autenticação do candidato."""

import signal

from zcode_worker import cli


def test_auth_failures_are_static_and_do_not_leak_stderr():
    for marker in ("project_token_login_failed", "login_required"):
        error = cli._classify_process_failure(1, f"{marker} token=synthetic-secret")
        assert error.code == "auth_required"
        assert error.args == ("autenticação necessária",)
        assert "synthetic" not in str(error)


def test_auth_classifier_preserves_quota_rate_limit_and_other_failures():
    assert cli._classify_process_failure(1, "business_session_storage_failed token=synthetic-secret").code == "auth_storage_failed"
    assert cli._classify_process_failure(1, "quota exhausted project_token_login_failed").code == "quota"
    assert cli._classify_process_failure(1, "HTTP 429 login_required").code == "rate_limit"
    assert cli._classify_process_failure(-signal.SIGTERM, "login_required").code == "terminated"
    assert cli._classify_process_failure(2, "unrelated failure").code == "cli_failed"


LAUNCHER_TARGETS = (
    "business request",
    "resolver store",
    "login store",
    "business invalidation",
    "native credential CAS",
    "async bundle lifecycle",
)
LAUNCHER_MESSAGE = "launcher de autenticacao incompativel com o bundle do ZCode"


def test_launcher_incompatible_matches_all_six_targets():
    for target in LAUNCHER_TARGETS:
        for stderr in (f"auth launcher patch drift: {target}", f"auth launcher patch drift: {target}\n"):
            error = cli._classify_process_failure(1, stderr)
            assert error.code == "launcher_incompatible"
            assert error.args == (LAUNCHER_MESSAGE,)
            assert target not in str(error)


def test_launcher_incompatible_rejects_prefix_suffix_secret_unknown_and_other_rc():
    base_stderr = "auth launcher patch drift: business request"
    negatives = (
        f"prefixo {base_stderr}",
        f"{base_stderr} detalhe",
        f"{base_stderr} token=synthetic-secret",
        "auth launcher patch drift: unknown target",
    )
    for stderr in negatives:
        error = cli._classify_process_failure(1, stderr)
        assert error.code != "launcher_incompatible"
        assert "synthetic" not in str(error)
    assert cli._classify_process_failure(2, base_stderr).code == "cli_failed"
    assert cli._classify_process_failure(-signal.SIGTERM, base_stderr).code == "terminated"


def test_error_result_keeps_exit_one_and_effects_flag_for_launcher_incompatible():
    error = cli._classify_process_failure(1, "auth launcher patch drift: login store")
    result, code = cli._error_result(error, 0.5, effects_may_exist=True)
    assert code == 1
    assert result["exitCode"] == 1
    assert result["error"]["code"] == "launcher_incompatible"
    assert result["error"]["message"].startswith(LAUNCHER_MESSAGE)
    assert result["effectsMayExist"] is True
    result, code = cli._error_result(error, 0.5)
    assert code == 1
    assert result["exitCode"] == 1
    assert result["effectsMayExist"] is False
