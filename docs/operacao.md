# Operação do worker-ask-router

Detalhe operacional do [README](../README.md): isolamento, fallback por estado,
apresentação, logs, cota, disponibilidade, cadeia de workers e validação.

## Isolamento por worktree

Opcional, desligado por padrão.

Com `WORKER_ASK_ISOLATION=write` (ou `all`, que inclui `--mode plan`), o worker roda numa cópia do projeto e não no diretório real. Também liga por projeto: arquivo `.worker-ask-isolation` com `write` ou `all` entre o `--cwd` e a raiz do repositório. A variável vence o arquivo; `off` desliga.

- **Cópia:** worktree git reaproveitado em `<state_dir>/worktrees/<repo>/slot-<n>` (até 8 por repositório; `WORKER_ASK_ISOLATION_SLOTS`, 1 a 32). A cada tentativa a cópia volta ao commit atual, recebe o estado não commitado do projeto (nunca arquivo de nome sensível nem repositório aninhado) e ganha atalhos para `node_modules`, `.venv` e `venv` ignorados pelo git (`WORKER_ASK_ISOLATION_LINKS`).
- **Devolução:** ao fim de cada tentativa, qualquer que seja o rc, só os `--allowed-file` alterados na cópia são gravados no projeto. Trabalho parcial de worker que falhou também volta. Se o arquivo mudou no projeto durante a execução, nada é sobrescrito: aviso `conflito na devolucao`, rc 1 e a versão do worker fica na cópia.
- **Fora do escopo:** arquivo alterado fora da lista não chega ao projeto; fica uma cópia em `<state_dir>/rejected/<run_id>/` por 7 dias (até 20 arquivos, 8 MiB).
- **Sem slot livre** por 120 s: para com erro (`isolamento sem slot livre`), sem rodar direto.
- **Quando não dá para isolar** (fora de git, repositório sem commit, `--cwd` ignorado pelo git, arquivo autorizado em repositório aninhado, estado não commitado acima de 512 MiB ou arquivo acima de 64 MiB): roda direto e registra `isolation: "none"` com o motivo. `WORKER_ASK_ISOLATION_STRICT=1` troca isso por rc 1.
- **Limite:** não é sandbox de sistema. Worker com shell ainda consegue escrever fora da cópia por caminho absoluto; o roteador avisa (`arvore real mudou durante a execucao`) e conta em `real_tree_changed`. Os atalhos de dependência apontam para os diretórios reais.
- **Operação:** `worker-ask --status` lista os slots; `worker-ask --isolation-gc` remove slots sem uso há 14 dias, slots de repositório que sumiu e cópias rejeitadas com mais de 7 dias. O `runs.jsonl` ganha `isolation`, `isolation_prep_s`, `overlay_files`, `sync_applied`, `sync_conflicts`, `oos_discarded` e `real_tree_changed`.

## Fallback por estado

`WORKER_ASK_STATE_FALLBACK=1`, só com isolamento, só em escrita: se um executor sem shell (ZCode, Muse, OpenCode) sai com rc 1 ou estoura o tempo e a cópia ficou idêntica (nenhum arquivo alterado, dentro ou fora do escopo) e os arquivos autorizados do projeto também, a cadeia segue para o próximo executor em vez de parar. A decisão usa só estado conferido, nunca texto de erro. Codex e Claude têm shell e continuam parando. Cada vez que isso acontece conta em `state_fallbacks`.

## Apresentação e log

`bin/ask_presentation.py` envolve o `worker-ask` e os wrappers `-ask`: stdout segue byte a byte (é a resposta), o código de saída é preservado e o stderr bruto dos wrappers continua privado. O roteador manda eventos estruturados por um pipe dedicado (`ASK_PRESENTATION_FD=fd:inode`, validado pelo inode e removido do ambiente dos wrappers); sem esse canal ele mantém as linhas antigas (`zcode-worker: rc=0`, `skip`, `fallback`). A diretiva de timeout sai sempre no stderr.

Em TTY, o progresso é uma única linha atualizada no lugar; fora de TTY há uma linha por mudança de estado e um batimento a cada `ASK_PRESENTATION_INTERVAL` segundos (padrão 60).

- `ASK_PRESENTATION=auto|plain|json|off`: `auto` usa cor e linha viva só em TTY; `plain` nunca usa cor; `json` emite eventos JSON por linha; `off` não exibe nada.
- `ASK_STYLE=compact|full|plain` (alias `WORKER_ASK_STYLE`): `full` acrescenta orquestrador, lista de arquivos, executores pulados, cache de tokens e sessão; `plain` equivale a `ASK_PRESENTATION=plain`.
- `NO_COLOR` desliga cor; `--title`/`ASK_TITLE` acrescentam um título ao cabeçalho.
- `worker-ask ... --log ARQ` substitui `> ARQ 2>&1; echo "exit=$?" >> ARQ`: grava stdout, stderr e a linha final `exit=N` (arquivo regular, sem symlink, criado com 0600), mantendo a saída no terminal.

## Log de execuções

Cada execução que chega à cadeia de workers grava uma linha JSON em `<state_dir>/runs.jsonl` (`$XDG_STATE_HOME/worker-ask/runs.jsonl` ou `~/.local/state/worker-ask/runs.jsonl`). Serve para medir retrabalho e custo por tarefa: guarda apenas métricas (executor final, rc, duração, tentativas de fallback, `spec_sha256`, tamanho do prompt e arquivos alterados). Nunca grava prompt, spec, stdout ou stderr dos workers. `--status`, `--help`, erros de uso e recursão bloqueada não geram linha. Desliga com `WORKER_ASK_RUNS_LOG=0`. Acima de 5 MiB o arquivo gira para `runs.jsonl.1` (um backup só). Falha de escrita emite no máximo um aviso no stderr e nunca muda rc, stdout ou `--log`.

```bash
python3 scripts/runs_report.py [--file PATH] [--days N] [--json]
```

## Admissão ZCode

Na admissão ZCode, auxiliares cujo pai já é candidato no mesmo grupo de processo não consomem uma vaga extra; um worker em grupo independente continua sendo contado. O prazo de fila é separado do prazo total: aumentar `worker-ask --timeout` não altera a fila padrão de 5 segundos.

## Cota e data de liberação

O roteador consulta as fontes locais de limite antes de encaminhar a tarefa quando não há bloqueio registrado. Para ZCode e Codex, usa as consultas sem geração do helper local `ask-limites-json`, quando instalado. O Codex usa o `CODEX_HOME` da chamada, sem assumir a conta da ponte. Para Claude, usa apenas cache de limite recente (até 30 minutos).

Ao receber rc 75, os wrappers preservam somente a data de reset ou `Retry-After` do provedor em metadados; diagnósticos brutos continuam privados. O cache guarda o prazo real, inclusive semanal ou mensal, e pula esse worker em todas as chamadas até vencer. Não há timer, sonda de modelo nem tentativa periódica antes da liberação.

`worker-ask --status` mostra `until` (epoch), `reset_known` e `ttl_remaining`. Se o provedor e a consulta não informarem data, `until=null` e `reset_known=false`: o worker fica bloqueado para não ser chamado repetidamente. Nesse caso a reavaliação é manual e explícita com `--retry-exhausted`; não se inventa um reset de 60 segundos. Uma execução bem-sucedida com essa opção limpa o bloqueio. Muse e OpenCode dependem da data presente na resposta de erro; não se usa uma chamada de modelo para sondar cota.

A disponibilidade pode mudar depois da consulta. Timeout não é cota: abre o circuito do executor por 300 segundos e depois permite nova tentativa; `blocked_reason=timeout` distingue esse caso da cota. Em escrita a chamada atual para para preservar possíveis alterações parciais. Entrada antiga de timeout sem prazo é ignorada. Cota desconhecida (`until=null`, `reset_known=false`) continua bloqueada. Os testes de cota usam HOME, provedores e CLIs sintéticos, sem contas.

## Tabela de disponibilidade

A tabela de disponibilidade (`availability.json`, gravada pelo timer `worker-ask-disponibilidade` ou por `worker-ask --refresh-availability`) tira a consulta de cota da chamada: o roteador lê o prazo de cada worker na tabela, sonda vagas ao vivo (ZCode e OpenCode) e entrega direto ao primeiro livre. Sem vaga, o skip sai com motivo `ocupado`; fila de admissão cheia aparece como `fila` e não grava cooldown. A fila do zcode-worker cai para 5 segundos (só margem de corrida) e o opencode-ask recebe `OPENCODE_ASK_WAIT=6` por padrão. Antes de lançar, o roteador reserva a vaga com flock (arquivos de trava no state dir), então chamadas simultâneas não disputam a mesma vaga: quem fica sem reserva sai como `ocupado` na hora. Ninguém livre: rc 75 imediato com o motivo de cada worker da cadeia. Tabela ausente mantém o comportamento anterior; tabela vencida ou inválida cai na consulta síncrona da chamada, com aviso (timer parado?).

## Cadeia de workers

O parâmetro `--orchestrator` identifica quem delega e não exclui workers da mesma plataforma. Codex também pode usar o worker Luna (`codex-ask`). A proteção contra recursão é `WORKER_ASK_ACTIVE=1`, propagada aos workers: eles não podem delegar outra tarefa ao roteador. O Muse é padrão na cadeia; o opt-out por projeto usa `--no-muse`, `WORKER_ASK_NO_MUSE=1` ou o arquivo `.worker-ask-no-muse` na raiz do projeto (usar em projeto com dado pessoal, base de clientes, dump ou segredo real). `--allow-muse` virou no-op, mantido por compatibilidade.

A cadeia vigente usa ZCode (só Start Plan), Muse (padrão; opt-out por projeto), OpenCode, Codex Luna, Claude Haiku e, por último, ZCode no plano pago (`zcode-pago`). O plano pago do ZCode só entra quando nenhum outro worker atendeu: o degrau `zcode-worker` nunca cai no pago por conta própria (Start sem cota, ocupado ou desligado = rc 75 e a cadeia segue); só o degrau `zcode-pago` exporta `ZCODE_WORKER_ALLOW_PAID=1`. Fora do roteador, `zcode-worker` com modelo do Start só usa o pago com essa variável definida. O `zcode-pago` fica sempre no fim, fora da ordem salva pelo painel.

## Validação e quality gate

Os testes geram o candidato automaticamente a partir do core versionado em `bin/zcode-worker`; não dependem de um artefato `out/` prévio.

Dependências de desenvolvimento usadas: pytest, coverage.py com suporte a subprocessos, Ruff e Gitleaks. Os testes de CAS nativo e aceitação do launcher requerem o bundle ZCode instalado em `/opt/ZCode/resources/glm/zcode.cjs`.

```bash
python3 -m pytest -q
cd adapters/zcode
python3 scripts/make_candidate.py
python3 -m pytest -q
node --test tests/auth-session.test.cjs
```

Na raiz:

```bash
python3 scripts/quality_gate.py run --root . --plan quality-gate.json --output quality-gate-result.json
python3 scripts/quality_gate.py verify --report quality-gate-result.json
```

A mutação é uma amostra explícita das invariantes do roteador e da autenticação, não prova exaustiva. O launcher valida pontos específicos do bundle e falha fechado quando uma atualização os altera.
