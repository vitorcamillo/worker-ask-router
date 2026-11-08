# ask-glm-bridge

Ponte HTTP (porta 10110, `ask-glm-bridge.service`) usada pelo Kairix. Só chama o `zcode-worker`, que decide Start Plan ou plano pago.
Instalar: `install -m 775 ask-glm-bridge.py ask_bridge_anthropic.py ~/.local/bin/ && systemctl --user restart ask-glm-bridge.service`.
Testes: `python3 -m pytest -q -p no:cacheprovider pontes/ask-glm-bridge`.
