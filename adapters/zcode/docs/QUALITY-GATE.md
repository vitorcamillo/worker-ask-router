# Quality Gate Universal dos Agentes

Este é o contrato de qualidade compartilhado por Claude, Codex e OpenCode. Ele transforma a
entrega de código em uma decisão baseada em evidência: nenhum agente pode declarar a tarefa
pronta enquanto o relatório do gate estiver incompleto, falhar ou possuir um bloco silencioso.

O gate não promete código perfeito — nenhum conjunto de testes prova isso. Ele reduz regressões
e impede que a IA esconda uma lacuna de validação atrás de uma resposta confiante.

## Regra operacional

Em toda mudança que altera código, configuração executável, contrato, dados ou comportamento:

1. classifique o risco e os arquivos alterados;
2. crie o plano antes de implementar;
3. crie ou atualize os testes aplicáveis antes/de junto da implementação;
4. rode os comandos declarados;
5. registre métricas e evidências;
6. execute `verify` e só então informe a conclusão.

O runner local é:

```bash
python3 scripts/quality_gate.py prepare --root . --output quality-gate.json
# preencher o plano com os comandos e cenários da mudança
python3 scripts/quality_gate.py run --plan quality-gate.json \
  --output quality-gate-result.json
python3 scripts/quality_gate.py verify --report quality-gate-result.json
```

Os comandos do plano são sempre listas `argv`, por exemplo
`["uv", "run", "pytest", "-q"]`. Nunca converter texto de saída do modelo em `shell=True`.

## Nove blocos obrigatórios

| Bloco | O que a IA deve criar/executar | Evidência mínima |
|---|---|---|
| `unit` | testes unitários de regras, limites, erros e invariantes | comando, resultado e casos novos |
| `gherkin` | cenários BDD/Gherkin para comportamento de negócio observável | `.feature` ou equivalente executado |
| `quality` | lint, formatter, typecheck, segurança, dependências, secrets e diff | comandos e falhas zeradas |
| `metrics` | pass rate, duração, escopo, risco e tendências do gate | JSON/relatório reproduzível |
| `mutation` | mutação nas linhas/regras alteradas e análise dos sobreviventes | score, mutantes mortos/sobreviventes e limiar |
| `coverage` | cobertura de linhas e branches, priorizando código alterado | relatório de cobertura e limiar |
| `acceptance` | critérios de aceite ponta a ponta da solicitação | matriz critério → cenário → resultado |
| `qa` | matriz positiva, negativa, regressão, segurança, limites e concorrência quando aplicável | casos executados e riscos residuais |
| `e2e` | fluxo automatizado na superfície real (CLI, HTTP, UI ou MCP) | comando, ambiente e resultado |

Gherkin e acceptance são complementares: o primeiro expressa comportamento em linguagem de
negócio; o segundo prova que cada critério solicitado foi atendido. QA é mais amplo e inclui
casos que não cabem no caminho feliz.

## Aplicabilidade sem atalhos

Todos os nove blocos devem aparecer no JSON. Um bloco só pode ser `N/A` quando a mudança não
possui aquela superfície. Nesse caso, o agente deve preencher:

```json
{
  "applicable": false,
  "status": "N/A",
  "reason": "Alteração interna sem superfície executável; não há fluxo E2E possível.",
  "evidence": ["docs/QUALITY-GATE.md#aplicabilidade-sem-atalhos"]
}
```

`NOT_RUN`, “não deu tempo”, “não achei ferramenta” ou “parece seguro” não são aprovação.
Quando a ferramenta necessária não existe, o estado é `BLOCKED`, com instalação/remediação
proposta. `FAIL` e `BLOCKED` bloqueiam a entrega.

Para alteração de lógica, `mutation` e `coverage` normalmente são obrigatórios. Se o projeto
não tiver um mutator instalado, registre o bloqueio e use uma alternativa explícita (testes de
propriedade/adversariais) apenas como mitigação, nunca como substituto silencioso.

## Seleção por ecossistema

Use as ferramentas já fixadas pelo projeto; os comandos abaixo são referências:

| Ecossistema | Unit/Gherkin/aceite/QA | Coverage | Mutation | E2E |
|---|---|---|---|---|
| Python | `pytest`, `pytest-bdd` | `pytest-cov` | `mutmut` ou `cosmic-ray` | Playwright, HTTP/CLI harness |
| Node/TS | Vitest/Jest, Cucumber | c8/istanbul | Stryker | Playwright/Cypress |
| Java | JUnit, Cucumber | JaCoCo | PIT | REST Assured/Selenium |
| Go | `go test` e cenários BDD | `go test -cover` | ferramenta adotada pelo repo | cliente HTTP/Playwright |
| Rust | `cargo test`, BDD adotado | llvm-cov/tarpaulin | cargo-mutants | harness HTTP/CLI |

Não instalar dependência globalmente ou trocar o framework do projeto sem registrar a decisão.
Versões e APIs atuais devem ser confirmadas na documentação oficial quando forem relevantes.

## Métricas mínimas

O relatório deve conter, no mínimo:

- taxa de aprovação unitária, Gherkin, acceptance, QA e E2E;
- cobertura de linhas e branches;
- mutation score, além da contagem de mutantes sobreviventes;
- duração por bloco, arquivos alterados, risco e falhas/retries;
- evidência rastreável: comando, saída resumida, artefato e data.

Não usar cobertura como sinônimo de qualidade: uma linha executada pode continuar errada. Não
usar mutation score como prova isolada: mutantes equivalentes e cenários ausentes continuam
exigindo análise humana do agente orquestrador.

## Instrução comum aos três harnesses

Claude, Codex e OpenCode devem aplicar o mesmo fluxo, independentemente do modelo escolhido:

- o modelo local pode fazer triagem e rascunho, mas não decide que o gate passou;
- DeepSeek e Luna são fallbacks de execução, não exceções ao gate;
- o agente principal mantém a arquitetura, confere o diff e valida a evidência;
- um worker só retorna sucesso quando a verificação declarada retorna zero e o relatório tem
  `STATUS: COMPILOU`/resultado equivalente;
- não apagar testes existentes para fazer o gate passar;
- não reduzir limiar, desativar mutação, mascarar falha ou marcar `N/A` sem motivo;
- registrar risco residual e limites reais, sem afirmar “perfeito”.

## Saída curta obrigatória do agente

Ao terminar, o agente deve devolver:

```text
QUALITY_GATE: PASS | FAIL | BLOCKED
CATEGORIAS: unit=PASS, gherkin=PASS|N/A, quality=PASS, metrics=PASS,
            mutation=PASS|N/A, coverage=PASS, acceptance=PASS,
            qa=PASS, e2e=PASS|N/A
METRICAS: coverage_line=..., coverage_branch=..., mutation_score=...
EVIDENCIA: comando principal + caminho do relatório
RISCO_RESIDUAL: ...
```

O texto é apenas um resumo. A fonte de verdade é `quality-gate-result.json` validado por
`quality_gate.py verify`.
