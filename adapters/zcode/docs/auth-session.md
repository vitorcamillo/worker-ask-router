# Sessão business persistente do ZCode

Este diretório contém a correção instalada no wrapper local em 16/10/2026.
O launcher lê o bundle oficial em memória, verifica sete trechos
com contagem exata e só então compila a fonte modificada. Qualquer drift aborta
com mensagem segura antes da execução do bundle.

## Persistência

`auth/zai-session.cjs` usa um único registro fixo da `credentialStore` nativa.
O valor salvo é criptografado pela store; o conteúdo lógico contém apenas o
fingerprint SHA-256 do OAuth, `data.access_token`, `expires_in` quando válido e
`expiresAt` absoluto. O token nunca é escrito em arquivo próprio, impresso ou
exposto em `globalThis`; o global do launcher contém somente funções do helper.

O cache exige fingerprint igual e mais de 120 segundos restantes. O prazo é o
menor entre `exp` do JWT, `expires_in` contado desde a requisição e 30 dias.
Quando o servidor não oferece nenhum desses prazos, aplica-se o limite
conservador explícito de 7 dias. Um retorno servido do cache recalcula
`expires_in` pelo tempo restante, sem estender a sessão.

`exp` presente, mas zero, negativo, textual, inválido ou expirado, é rejeitado;
isso não vira fallback de 7 dias. A resposta aceita também `accessToken` e
`expiresIn`, normalizando-os antes da persistência.

Atualizações de um registro observado usam `saveIfValue`, exposto pelo patch
nativo dentro de `H$`; a comparação e a criptografia acontecem na mesma
transação da store. Uma resposta antiga não sobrescreve uma sessão nova de
outro processo. OAuth pronto de Z.ai é salvo antes da resolução business; a
gravação posterior existente permanece idempotente.

O launcher mantém `process.argv[1]` apontando para o bundle durante todo o
`main` assíncrono e restaura o argv original somente quando a Promise termina.
O bundle oficial é exercitado exclusivamente em subprocesso separado.

Falhas de timeout, 429 e 5xx não apagam o registro. A invalidação de rejeição
usa `deleteIfValue`, comparando fingerprint e token, para preservar uma sessão
nova que tenha vencido uma requisição concorrente. Cache malformado é tratado
como miss; erro da store vira somente `business_session_storage_failed`.

## Cenários Gherkin e ligação nominal

- Scenario: request inicial — Test: `tests/auth-session.test.cjs` / `request inicial salva somente a sessão mínima`
- Scenario: cache entre processos — Test: `tests/auth-session.test.cjs` / `cache fake atravessa processos sem expor o valor`
- Scenario: CAS entre processos — Test: `tests/auth-session.test.cjs` / `dois processos com resultados fora de ordem preservam a sessão nova via CAS`
- Scenario: validade JWT e expires_in — Test: `tests/auth-session.test.cjs` / `exp JWT e expires_in limitam o prazo`
- Scenario: aliases e exp inválido — Test: `tests/auth-session.test.cjs` / `accessToken/expiresIn são aceitos e prazo JWT inválido não ganha fallback`
- Scenario: validação e fingerprint — Test: `tests/auth-session.test.cjs` / `resposta inválida nunca é salva`
- Scenario: cleanup da trava — Test: `tests/auth-session.test.cjs` / `cleanup da trava local não deixa rejeição em voo`
- Scenario: concorrência — Test: `tests/auth-session.test.cjs` / `requisições concorrentes para a mesma conta são deduplicadas`
- Scenario: invalidação CAS — Test: `tests/auth-session.test.cjs` / `invalidação CAS não remove sessão nova e falha transitória preserva cache`
- Scenario: CAS de save — Test: `tests/auth-session.test.cjs` / `CAS de save preserva resultado mais novo fora de ordem`
- Scenario: storage failure — Test: `tests/auth-session.test.cjs` / `cache malformado é tratado como miss e storage failure é seguro`
- Scenario: drift fail closed — Test: `tests/auth-session.test.cjs` / `launcher falha fechado em drift e aplica os pontos de integração da fixture`
- Scenario: shell paths — Test: `tests/auth-session.test.cjs` / `candidate preserva os caminhos estáveis do launcher e da admissão`

## Execução

```bash
node tests/auth-session.test.cjs
node --experimental-test-coverage --test-coverage-include=auth/*.cjs tests/auth-session.test.cjs
pytest -q tests/test_auth_session.py
```

Os testes usam somente tokens sintéticos e stores em memória/temporários. A
validação real deve ser executada pelo orquestrador; este candidato não altera
o bundle instalado nem solicita login.
