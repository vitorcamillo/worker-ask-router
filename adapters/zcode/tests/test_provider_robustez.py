from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

import pytest

from zcode_worker import base, features


def write_provider(path: Path, provider_id: str):
    selection = {"providerId": provider_id, "modelId": "GLM-5.3-Flash", "options": {"reasoningLevel": "low"}}
    path.write_text(json.dumps({"defaultModelSelection": selection}), encoding="utf-8")


def test_config_node_substitui_provider_estranho():
    config = {
        "defaultModelSelection": {
            "providerId": "claude-code",
            "modelId": "claude-sonnet",
            "options": {"reasoningLevel": "low", "temperature": 0.5},
        }
    }
    original = copy.deepcopy(config)
    selection = base._config_node(config)
    assert selection == {"providerId": base.PROVIDER_ID}
    assert "options" not in selection
    assert config["defaultModelSelection"] == {"providerId": base.PROVIDER_ID}
    # Nada das options do provider alheio sobrevive.
    assert json.dumps(original) != json.dumps(config)


def test_config_node_mantem_options_zai():
    config = {
        "defaultModelSelection": {
            "providerId": "account:zai-start-plan",
            "modelId": "GLM-5.3-Flash",
            "options": {"reasoningLevel": "low"},
        }
    }
    selection = base._config_node(config)
    assert selection["providerId"] == base.PROVIDER_ID
    assert selection["modelId"] == "GLM-5.3-Flash"
    assert selection["options"] == {"reasoningLevel": "low"}


def test_config_node_bigmodel_tambem_e_reaproveitado():
    config = {"defaultModelSelection": {"providerId": "account:bigmodel-coding", "options": {"reasoningLevel": "max"}}}
    selection = base._config_node(config)
    assert selection["providerId"] == base.PROVIDER_ID
    assert selection["options"] == {"reasoningLevel": "max"}


@pytest.mark.parametrize("config", [None, "x", 42, []])
def test_config_node_invalido(config):
    with pytest.raises(base.WorkerError, match="provider_config inválido"):
        base._config_node(config)


def test_carregar_config_provider_aceita_selecao_estrana(tmp_path, monkeypatch):
    provider = tmp_path / "provider.json"
    write_provider(provider, "chatgpt-codex")
    monkeypatch.setenv("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", str(provider))
    config = base.carregar_config_provider("glm-5.3-flash", "high")
    selection = config["defaultModelSelection"]
    assert selection["providerId"] == base.PROVIDER_ID
    assert selection["modelId"] == "GLM-5.3-Flash"
    assert selection["options"]["reasoningLevel"] == "high"
    assert set(selection["options"]) == {"reasoningLevel"}


def test_temporary_provider_remove_orfaos_vencidos(tmp_path, monkeypatch):
    provider = tmp_path / "provider.json"
    write_provider(provider, base.PROVIDER_ID)
    monkeypatch.setattr(features.base, "PROVIDER_CONFIG", str(provider))
    state = tmp_path / "state"
    tmp_dir = state / "tmp"
    tmp_dir.mkdir(parents=True)
    tmp_dir.chmod(0o700)

    antigo = time.time() - features.PROVIDER_TMP_TTL_SECONDS - 3600
    orfa = tmp_dir / "provider-velho.json"
    orfa.write_text("{}", encoding="utf-8")
    os.utime(orfa, (antigo, antigo))
    recente = tmp_dir / "provider-novo.json"
    recente.write_text("{}", encoding="utf-8")
    outro = tmp_dir / "outro.json"
    outro.write_text("{}", encoding="utf-8")
    os.utime(outro, (antigo, antigo))

    alvo = tmp_dir / "alvo-fora.json"
    alvo.write_text("{}", encoding="utf-8")
    os.utime(alvo, (antigo, antigo))
    link = tmp_dir / "provider-link.json"
    link.symlink_to(alvo)
    os.utime(link, (antigo, antigo), follow_symlinks=False)

    with features.temporary_provider("glm-5.3-flash", "high", state_dir=state) as path:
        assert not orfa.exists()
        assert recente.exists()
        assert outro.exists()
        assert link.is_symlink()
        assert alvo.exists()
        # O provider recém-criado sobrevive à própria limpeza.
        assert Path(path).exists()
        assert Path(path).name.startswith("provider-")
    assert not Path(path).exists()


def _store_dir(tmp_path: Path) -> Path:
    directory = tmp_path / ".zcode" / "v2"
    directory.mkdir(parents=True)
    return directory


def test_chave_local_do_store_e_criada_uma_vez_e_privada(tmp_path):
    directory = _store_dir(tmp_path)
    features.ensure_credential_secret(str(tmp_path))
    target = directory / "credential-secret"
    first = target.read_text(encoding="ascii")
    # limites que a CLI exige: 32..4096 bytes, sem permissão para grupo/outros
    assert 32 <= len(first) <= 4096
    assert target.stat().st_mode & 0o077 == 0
    features.ensure_credential_secret(str(tmp_path))
    assert target.read_text(encoding="ascii") == first
    assert sorted(p.name for p in directory.iterdir()) == ["credential-secret"]


def test_chave_local_nao_toca_store_do_keyring_nem_base_ausente(tmp_path):
    directory = _store_dir(tmp_path)
    (directory / "credential-source").write_text("os\n", encoding="ascii")
    features.ensure_credential_secret(str(tmp_path))
    assert not (directory / "credential-secret").exists()
    features.ensure_credential_secret(str(tmp_path / "ausente"))
    assert not (tmp_path / "ausente").exists()


def test_config_do_worker_nao_deixa_provider_de_terceiro_como_fallback():
    alheio, pago = "claude-code", "account:zai-individual-coding-plan"
    config = {
        "providerOrder": [alheio, pago],
        "providerConfigRules": {"providerRules": [{"providerId": alheio}, {"providerId": pago}]},
        "modelConfigRules": {
            "providerModelRules": [{"providerId": alheio}, {"providerId": pago}],
            "manualProviderModelRules": [{"providerId": alheio}],
        },
    }
    features._only_worker_providers(config)
    assert config["providerOrder"] == [pago]
    assert config["providerConfigRules"]["providerRules"] == [{"providerId": pago}]
    assert config["modelConfigRules"]["providerModelRules"] == [{"providerId": pago}]
    assert config["modelConfigRules"]["manualProviderModelRules"] == []
