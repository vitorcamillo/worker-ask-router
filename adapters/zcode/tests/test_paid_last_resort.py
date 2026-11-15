"""Regra: modelo do Start Plan só usa o plano pago com ZCODE_WORKER_ALLOW_PAID."""
from __future__ import annotations
import datetime
import pytest
from zcode_worker import base, features
from test_start_plan import _run_probe_integration, _run_with_mocks

NOW = datetime.datetime(2026, 10, 15, 13, 20)
OK = (0, '{"sessionId":"sess_1","response":"ok"}', "")


@pytest.fixture(autouse=True)
def _sem_pago(monkeypatch):
    monkeypatch.delenv("ZCODE_WORKER_ALLOW_PAID", raising=False)


def test_paid_allowed_so_com_valor_explicito():
    for value in ("1", "on", "TRUE", " yes "):
        assert features.paid_allowed({"ZCODE_WORKER_ALLOW_PAID": value}) is True
    for env in ({}, {"ZCODE_WORKER_ALLOW_PAID": "0"}, {"ZCODE_WORKER_ALLOW_PAID": ""}, {"ZCODE_WORKER_ALLOW_PAID": "x"}):
        assert features.paid_allowed(env) is False


@pytest.mark.parametrize("env", [{}, {"ZCODE_WORKER_START_PLAN": "off"}])
def test_provider_for_start_fora_do_ar_vira_cota(tmp_path, env):
    s = tmp_path / "state"
    features.mark_start_plan_unavailable("quota", now=NOW, state_dir=s)
    with pytest.raises(features.FeatureError) as erro:
        features.provider_for("glm-5.3-flash", now=NOW, state_dir=s, environ=env)
    assert erro.value.code == "quota"
    # Modelo fora do Start não tem alternativa: segue no plano da conta.
    assert features.provider_for("glm-5.3", now=NOW, state_dir=s, environ=env) == base.PROVIDER_ID


def test_start_livre_continua_start_sem_liberar_pago(tmp_path):
    assert features.provider_for("glm-5.3-flash", now=NOW, state_dir=tmp_path / "s", environ={}) == features.START_PLAN_PROVIDER_ID


@pytest.mark.parametrize("codigo", ["quota", "rate_limit", "start_plan_drift"])
def test_falha_do_start_na_execucao_sai_75_sem_tentar_pago(tmp_path, monkeypatch, codigo):
    result, code, calls = _run_with_mocks(
        tmp_path, monkeypatch, mode="plan", responses=[features.FeatureError("x", codigo), OK]
    )
    assert code == 75 and result["error"]["code"] == "quota"
    assert calls["n"] == 1 and calls["providers"] == [features.START_PLAN_PROVIDER_ID]


@pytest.mark.parametrize("codigo", ["quota", "rate_limit", "start_plan_drift"])
def test_sonda_do_start_falha_sai_75_sem_executar(tmp_path, monkeypatch, codigo):
    result, code, probe_calls, main_calls = _run_probe_integration(
        tmp_path, monkeypatch, probe_side=features.FeatureError("x", codigo), main_ok_provider=base.PROVIDER_ID
    )
    assert code == 75 and probe_calls["n"] == 1 and main_calls["n"] == 0


def test_sonda_atendida_pelo_pago_sai_75_sem_executar(tmp_path, monkeypatch):
    pago = {"providerId": base.PROVIDER_ID, "modelId": "GLM-5.3-Flash", "effort": "high"}
    result, code, _, main_calls = _run_probe_integration(
        tmp_path, monkeypatch, probe_side=({"sessionId": "sess_probe", "response": "ok"}, pago),
        main_ok_provider=base.PROVIDER_ID,
    )
    assert code == 75 and main_calls["n"] == 0
