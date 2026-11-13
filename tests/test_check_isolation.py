"""Regressão do gate de mutação: falha de infraestrutura bloqueia, não vira sobrevivente."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent


def _modulo():
    spec = importlib.util.spec_from_file_location(
        "check_isolation_sob_teste", ROOT / "scripts" / "check_isolation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _carregar(mod, monkeypatch, tmp_path, resposta):
    """mutation() com _pytest falso: resposta(1) é a baseline; resposta(n>1), o mutante n-1."""
    monkeypatch.setattr(mod, "OUT", tmp_path / "out")
    monkeypatch.setattr(mod, "_repo_files",
                        lambda: [mod.ISO_REL, mod.ROUTER_REL, "tests/test_isolation.py"])
    chamadas = []

    def falso_pytest(stage, test_files, timeout):
        chamadas.append(list(test_files))
        return SimpleNamespace(**resposta(len(chamadas)))

    monkeypatch.setattr(mod, "_pytest", falso_pytest)
    return chamadas


def _ok():
    return {"returncode": 0, "stdout": "", "stderr": ""}


def test_mutation_erro_de_coleta_bloqueia_com_rc2(tmp_path, monkeypatch, capsys):
    mod = _modulo()
    _carregar(mod, monkeypatch, tmp_path, lambda n: _ok() if n == 1 else {
        "returncode": 2, "stdout": "ERROR collecting tests/test_isolation.py\nInterrupted: 1 error",
        "stderr": ""})
    assert mod.mutation() == 2
    saida = capsys.readouterr().out
    assert "falha de infraestrutura no mutante" in saida
    assert not (tmp_path / "out" / "mutation.json").exists()


def test_mutation_rc_fora_de_0_e_1_bloqueia_com_rc2(tmp_path, monkeypatch, capsys):
    mod = _modulo()
    _carregar(mod, monkeypatch, tmp_path,
              lambda n: _ok() if n == 1 else {"returncode": 5, "stdout": "no tests ran", "stderr": ""})
    assert mod.mutation() == 2
    assert "falha de infraestrutura" in capsys.readouterr().out


def test_mutation_morto_e_sobrevivente_contam_normal(tmp_path, monkeypatch, capsys):
    mod = _modulo()

    def resposta(n):
        if n == 1:
            return _ok()  # baseline
        if n == 2:  # primeiro mutante morto
            return {"returncode": 1, "stdout": "FAILED tests/test_isolation.py::x - assert",
                    "stderr": ""}
        return _ok()  # demais sobrevivem

    _carregar(mod, monkeypatch, tmp_path, resposta)
    assert mod.mutation() == 1  # 1/37 morto: nota abaixo do mínimo, mas sem bloqueio de infra
    relatorio = json.loads((tmp_path / "out" / "mutation.json").read_text(encoding="utf-8"))
    assert relatorio["mutation_score_pct"] == 2.7
    assert relatorio["results"][0]["killed"] is True
    assert relatorio["results"][1]["killed"] is False
    assert "falha de infraestrutura" not in capsys.readouterr().out
