# worker-ask

Repositório único do roteador e de todos os wrappers `-ask` locais. Não contém código do iptzao nem de outro projeto.

## Fontes

- `worker_router.py`: seleção de executores, timeout total, cooldown e verificação dos arquivos autorizados.
- `bin/codex-ask`, `claude-ask`, `muse-ask`, `opencode-ask` e `bonsai-ask`: wrappers de cada sistema.
- `bin/ask-doctor` e `ask-check`: diagnóstico de interfaces e atualizações; o modo completo do doctor faz chamadas reais.
- `scripts/install.py`: instalação conjunta e repetível, com backups de código e rollback.
- `adapters/bonsai/`: cliente e guardas GameMode/GPU, sem chave, modelo ou serviço provisionado.
- `bin/ask_presentation.py`: apresentação do terminal.
- `adapters/zcode/`: engine, persistência de autenticação, launcher e testes do ZCode.
- `bin/zcode-worker`, `bin/zcode-cli` e `bin/zcode_headless_admit.py`: entrada local e admissão de processos.

Os binários nativos de Codex, Claude, Muse e OpenCode e o backend Bonsai continuam sendo dependências externas instaladas no sistema. O bundle proprietário do ZCode também é externo. Credenciais, sessões, bancos, caches e relatórios gerados não entram no Git.

## Instalação conjunta

```bash
python3 scripts/install.py
# Para verificar em um diretório de staging, sem alterar a instalação atual:
python3 scripts/install.py --home /caminho/de/staging
```

O instalador valida todas as fontes e destinos antes de substituir código, recusa pais simbólicos, diretórios e destinos compartilhados por hardlink, e usa trava contra instalações concorrentes. Os links são trocados atomicamente por arquivo; uma falha tratada tenta restaurar os arquivos anteriores; restauração incompleta é um erro explícito e mantém os backups. Backups de código ficam em `~/.local/state/worker-ask/install-backups/`. Um encerramento forçado ou queda de energia pode deixar a instalação parcial: execute novamente ou restaure os backups. O core ZCode permanece arquivo regular. Credenciais, sessões, catálogos de conta, configurações, serviços e modelos não são instalados nem migrados.

A guarda Bonsai requer PyGObject/Gio (`gi`) no Python do sistema. A inclusão de `bonsai-ask` não muda a cadeia do roteador: Bonsai permanece disponível diretamente, com suas guardas de jogo/GPU.

## Uso

Python 3.10+, Linux, executores instalados e autenticados fora do repositório. Node é necessário para o launcher ZCode. O comando instalado `~/.local/bin/worker-ask` aponta para `worker_router.py` desta raiz.

```bash
worker-ask --cwd /caminho/do/projeto --orchestrator codex --mode plan "tarefa"
worker-ask --cwd /caminho/do/projeto --orchestrator codex --mode write --allowed-file src/exemplo.py "tarefa"
```

O roteador pula o executor da plataforma orquestradora. Fallback automático ocorre somente em saída 75 ou executável ausente. Saída 1 para; incompatibilidade conhecida do launcher apresenta diagnóstico estático, sem imprimir stderr privado. Em escrita, mudanças nos arquivos autorizados após saída 75 bloqueiam o fallback. A lista de arquivos é verificação de estado, não sandbox.

### Apresentação e log

`bin/ask_presentation.py` envolve o `worker-ask` e os wrappers `-ask`: stdout segue byte a byte (é a resposta), o código de saída é preservado e o stderr bruto dos wrappers continua privado. O roteador manda eventos estruturados por um pipe dedicado (`ASK_PRESENTATION_FD=fd:inode`, validado pelo inode e removido do ambiente dos wrappers); sem esse canal ele mantém as linhas antigas (`zcode-worker: rc=0`, `skip`, `fallback`). A diretiva de timeout (`NAO assuma a tarefa...`) sai sempre no stderr.

```text
◆ Workers · escrita · …/draft/busca-games · 3 arquivos · spec-fatia5.md · limite 40min
  ↳ ZCode (glm-5.3-flash)
  ↷ ZCode: cota, tentando o próximo
  ↳ OpenCode (auto)
✓ OpenCode (auto) · 3m02s · validação pendente
  tentativas: ZCode cota (rc 75) → OpenCode ✓
```

Em TTY, o progresso é uma única linha atualizada no lugar; fora de TTY há uma linha por mudança de estado e um batimento a cada `ASK_PRESENTATION_INTERVAL` segundos (padrão 60).

- `ASK_PRESENTATION=auto|plain|json|off`: `auto` usa cor e linha viva só em TTY; `plain` nunca usa cor; `json` emite eventos JSON por linha; `off` não exibe nada.
- `ASK_STYLE=compact|full|plain` (alias `WORKER_ASK_STYLE`): `full` acrescenta orquestrador, lista de arquivos, executores pulados, cache de tokens e sessão; `plain` equivale a `ASK_PRESENTATION=plain`.
- `NO_COLOR` desliga cor; `--title`/`ASK_TITLE` acrescentam um título ao cabeçalho.
- `worker-ask ... --log ARQ` substitui `> ARQ 2>&1; echo "exit=$?" >> ARQ`: grava stdout, stderr e a linha final `exit=N` (arquivo regular, sem symlink, criado com 0600), mantendo a saída no terminal.

### Log de execucoes

Cada execucao que chega a cadeia de workers grava uma linha JSON em `<state_dir>/runs.jsonl` (`$XDG_STATE_HOME/worker-ask/runs.jsonl` ou `~/.local/state/worker-ask/runs.jsonl`). Serve para medir retrabalho e custo por tarefa: guarda apenas metricas (executor final, rc, duracao, tentativas de fallback, `spec_sha256`, tamanho do prompt e arquivos alterados). Nunca grava prompt, spec, stdout ou stderr dos workers. `--status`, `--help`, erros de uso e recursao bloqueada nao geram linha. Desliga com `WORKER_ASK_RUNS_LOG=0`. Acima de 5 MiB o arquivo gira para `runs.jsonl.1` (um backup so). Falha de escrita emite no maximo um aviso no stderr e nunca muda rc, stdout ou `--log`.

```bash
python3 scripts/runs_report.py [--file PATH] [--days N] [--json]
```

Na admissão ZCode, auxiliares cujo pai já é candidato no mesmo grupo de processo não consomem uma vaga extra; um worker em grupo independente continua sendo contado. O prazo de fila é separado do prazo total: aumentar `worker-ask --timeout` não altera a fila padrão de 30 segundos.

## Validação

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

## Fontes ativas e continuidade

As fontes Python, scripts e autenticação em `~/.local/share/zcode-worker/` foram vinculadas a `adapters/zcode/`; os auxiliares em `~/.local/bin/` apontam para `bin/`. O core instalado `~/.local/bin/zcode-worker` permanece arquivo regular, copiado de `bin/zcode-worker`, porque o gerador de candidato exige arquivo regular. Após alterar esse core, validar e reinstalar a cópia, preservando modo executável. O core procura o pacote desta raiz quando executado aqui e usa o caminho instalado como alternativa.

`migration-sources.json` registra a origem dos arquivos importados. O checkout ativo fica em `$HARNESS_ROOT/worker-ask` (raiz do harness local); a cópia em `harness/shared/harness-contracts` é legado. Backups da migração ficam apenas em `~/.local/state/worker-ask/source-migration-backup/`. Não mover o checkout enquanto os links estiverem ativos. O repositório não provisiona login, CLIs nativas, modelos ou serviços. Para instalar seus wrappers em outra máquina, clone este repositório e use `scripts/install.py`; prepare as dependências externas separadamente.

## Validação conjunta (26/10/2026)

O comando padrão na raiz passou com 676 testes e 26 subtestes; a suíte Node de autenticação passou em 18 casos. O quality gate passou nos nove blocos. Cobertura medida: 88,68% de linhas e 91,39% de branches, incluindo o roteador, o instalador e as funções de contagem de admissão. Mutação direcionada: 30/32 detectadas (93,75%, limiar 80%); os sobreviventes legados são `finite-time` e `no-symlink`. O instalador detectou 5/5 e a admissão 8/8; a amostra não é exaustiva. As funções alteradas de admissão atingiram 100% de linhas e branches.

Uma chamada real ZCode após a correção de admissão respondeu OK com rc=0 em 28,5 segundos. Os demais provedores foram exercitados com CLIs/servidores sintéticos, sem gastar cota ou acessar credenciais reais. A disponibilidade dos serviços e contas é externa a este repositório.

## Cota e data de liberacao

O roteador consulta as fontes locais de limite antes de encaminhar a tarefa quando nao
ha bloqueio registrado. Para ZCode e Codex, usa as consultas sem geracao do helper local
`ask-limites-json`, quando instalado. O Codex usa o `CODEX_HOME` da chamada, sem assumir a
conta da ponte. Para Claude, usa apenas cache de limite recente (ate 30 minutos).

Ao receber rc75, os wrappers preservam somente a data de reset ou `Retry-After` do
provedor em metadados; diagnosticos brutos continuam privados. O cache guarda o prazo
real, inclusive semanal ou mensal, e pula esse worker em todas as chamadas ate vencer.
Nao ha timer, sonda de modelo nem tentativa periodica antes da liberacao.

`worker-ask --status` mostra `until` (epoch), `reset_known` e `ttl_remaining`.
Se o provedor e a consulta nao informarem data, `until=null` e `reset_known=false`:
o worker fica bloqueado para nao ser chamado repetidamente. Nesse caso a reavaliacao
manual e explicita com `--retry-exhausted`; nao se inventa um reset de 60 segundos.
Uma execucao bem-sucedida com essa opcao limpa o bloqueio. Muse e OpenCode dependem da
data presente na resposta de erro; nao se usa uma chamada de modelo para sondar cota.

A disponibilidade pode mudar depois da consulta. Timeout nao e cota: abre o circuito
do executor por 300 segundos e depois permite nova tentativa;
`blocked_reason=timeout` distingue esse caso da cota. Em escrita a chamada atual para
para preservar possiveis alteracoes parciais. Entrada antiga de timeout sem prazo
e ignorada. Cota desconhecida (`until=null`, `reset_known=false`) continua bloqueada.
Os testes de cota usam HOME, provedores e CLIs sinteticos, sem contas.

O parametro `--orchestrator` identifica quem delega e nao exclui workers da mesma plataforma.
Codex tambem pode usar o worker Luna (`codex-ask`). A protecao contra recursao e
`WORKER_ASK_ACTIVE=1`, propagada aos workers: eles nao podem delegar outra tarefa ao roteador.
O Muse e padrao na cadeia; o opt-out por projeto usa `--no-muse`, `WORKER_ASK_NO_MUSE=1` ou o arquivo `.worker-ask-no-muse` na raiz do projeto (usar em projeto com dado pessoal, base de clientes, dump ou segredo real). `--allow-muse` virou no-op, mantido por compatibilidade.

A cadeia vigente usa ZCode, Muse (padrao; opt-out por projeto), OpenCode, Codex Luna e Claude Haiku.
O degrau Terra foi retirado da cadeia; seu handler antigo permanece apenas por
compatibilidade. Esta politica substitui a cadeia anunciada no commit 07cb87c.
