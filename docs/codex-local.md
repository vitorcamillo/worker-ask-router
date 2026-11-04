# codex-local — experimento histórico (fora do roteador)

> Aviso: `codex-local` é um experimento histórico. Está fora do roteador
> `worker-ask`: não há seleção automática, opt-in, flags `--local`,
> `--local-model` ou `--local-base-url`, nem cadeia isolada. A cadeia ativa
> é ZCode → Muse → OpenCode → Codex → Claude.

O wrapper `bin/codex-local` permanece no repositório apenas como artefato
histórico e ferramenta de diagnóstico experimental standalone. Não é caminho
recomendado para execução de fatias.

## O que foi removido do roteador

Comandos históricos abaixo **não funcionam mais** e são recusados com saída 2:

```sh
# HISTÓRICO — indisponível: o roteador não aceita mais --local.
worker-ask --orchestrator codex --local --cwd PROJETO --mode write \
  --allowed-file src/exemplo.py --spec tarefa.json --timeout 300
```

O mesmo vale para `--local-model` e `--local-base-url`, isoladas ou
combinadas: qualquer uma delas causa recusa (rc=2) antes de executar
qualquer worker.

## Uso atual de modelo local

O perfil de uso atual de modelo local vive no projeto Jarvis/Kairix, no
módulo `local_llm`. Consulte a documentação daquele projeto para o fluxo
suportado. Este documento não descreve ferramentas daquele módulo além do
nome, para não inventar interface ainda não consolidada aqui.

## Diagnóstico experimental standalone (não recomendado)

Os exemplos abaixo chamam o wrapper diretamente, sem passar pelo roteador,
apenas para diagnóstico experimental:

```sh
codex-local --template
codex-local --cwd PROJETO --spec tarefa.json --dry-run
codex-local --cwd PROJETO --mode write --spec tarefa.json --timeout 300
```

`--dry-run` faz preflight de servidor/CLI e retorna diagnóstico JSON sem
chamar inferência. Spec inválida retorna 2 antes do modelo; falha de
servidor retorna 1 sem fallback cloud. Para o contrato de entrada/saída
detalhado do wrapper, ver a revisão histórica do documento e o exemplo em
[codex-local-spec.example.json](./codex-local-spec.example.json).

Caminhos locais relacionados (históricos): `bin/codex-local`,
`docs/codex-local-spec.example.json`, `tests/test_codex_local.py`,
`scripts/check_codex_local_mutation.py`, `scripts/export_metrics.py`.
