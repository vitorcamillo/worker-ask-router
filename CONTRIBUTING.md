# Como contribuir

Contribuições são bem-vindas. Abra uma proposta pequena e focada, descrevendo o
comportamento esperado e como validou a mudança. Preserve os limites e as
garantias documentados no [README](README.md).

## Validar mudanças

Execute os testes relevantes para a alteração. Os comandos já usados neste
repositório incluem:

```bash
python3 -m pytest -q
cd adapters/zcode
python3 scripts/make_candidate.py
python3 -m pytest -q
node --test tests/auth-session.test.cjs
```

O README descreve dependências e limitações, inclusive os testes que requerem o
bundle ZCode instalado. O quality gate pode ser executado conforme os comandos
na seção Validação do README.

Não inclua credenciais, sessões, bancos, configurações locais de provedores ou
dados pessoais em commits, issues, prompts ou chamadas a provedores. Use dados
sintéticos para reproduções e testes.
