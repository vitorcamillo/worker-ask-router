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
