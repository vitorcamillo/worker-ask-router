# ask-glm-bridge

Ponte HTTP (porta 10110, `ask-glm-bridge.service`) usada pelo Kairix. Só chama o `zcode-worker`, que decide Start Plan ou plano pago.
Pré-requisito: a ponte passa `--json` (sempre) e `--chat` (modo plan) ao worker — o `zcode-worker` efetivo precisa declarar essas flags (confira com `--help`). O par `bin/zcode-worker` + `adapters/zcode/src` deste repo, instalado como `~/.local/bin/zcode-worker` + `~/.local/share/zcode-worker/src`, atende; outro executável pode ser apontado com `ASK_GLM_ZCODE_WORKER`.
Instalar: `install -m 755 ask-glm-bridge.py ~/.local/bin/ && install -m 644 ask_bridge_anthropic.py ~/.local/bin/ && systemctl --user restart ask-glm-bridge.service`.
Testes: `python3 -m pytest -q -p no:cacheprovider pontes/ask-glm-bridge`.
