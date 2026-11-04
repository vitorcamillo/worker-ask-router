# zcode-worker

Este guia descreve a versão complementar do comando `zcode-worker`, validada para integração pelo orquestrador.

## Tarefa foreground

Uma tarefa pode vir como argumento, por stdin ou por arquivo:

```bash
zcode-worker --cwd "$PWD" "descreva a tarefa"
printf '%s\n' 'descreva a tarefa' | zcode-worker --cwd "$PWD"
zcode-worker --cwd "$PWD" --spec tarefa.md
```

`--mode` aceita `plan`, `edit` ou `yolo`. O padrão do worker é `edit` no
launcher local. O modo `edit` mantém a denylist headless; `yolo` não aplica essa
denylist automaticamente. A fila global continua serializada pela CLI nativa,
mesmo que a configuração de `max_parallel` tenha mais de um slot.

## Modelo, effort e perfis

Use `--model glm-5.3-flash` ou `--model glm-5.3` e `--effort low|high|max`.
Os perfis são `--profile rapido`, `--profile execucao` e `--profile raciocinio`.
Sem perfil, o padrão é **GLM-5.3-Flash com effort high**. A admissão exige
MemAvailable de pelo menos 4 GiB e um slot global; a CLI global permanece
independente do `max_parallel` da configuração. A fila global é serializada e
pode ser não-FIFO; o worker não promete paralelismo efetivo além da trava nativa.

`--timeout` limita a execução, `--queue-timeout` limita a espera por slot e
`--total-timeout` limita o conjunto fila + execução + retries. Os valores são
finitos e positivos, até 3600 segundos. Retries existem apenas para rate limit
no modo `plan`; não há orçamento de tokens inventado pelo worker.

Em `edit` e `yolo`, um erro depois da chamada da CLI pode conter
`effectsMayExist: true`. Antes de qualquer fallback ou nova tentativa, revise o
diff: o wrapper não promete rollback e informa isso com a mensagem fixa
`Arquivos podem ter sido alterados; revise o diff antes de tentar novamente.`.
Falhas de argumento, tarefa, resume, configuração, fila, `dry-run` e `plan`
mantêm `effectsMayExist: false`.

## JSON, anexos e denylist

`--json` imprime um objeto estruturado. `--max-output-bytes` limita a coleta de
stdout/stderr; o limite padrão é 16 MiB. `--attach ARQUIVO` pode ser repetido para anexos regulares
`.png`, `.jpg`, `.jpeg`, `.webp`, `.pdf`, `.txt`, `.md` ou `.json`, até cinco
arquivos e 10 MiB por arquivo. Symlinks, arquivos secretos e tipos não
permitidos são recusados.

`--disallowed-tools NOME` pode ser repetido ou receber nomes separados por
vírgula. A lista aceita apenas nomes simples de ferramentas e remove duplicatas.

## Resume, diagnóstico e dry-run

`--resume sess_ID` só aceita sessão que pertença ao mesmo `cwd` e cujo modelo e
effort efetivos coincidam com o pedido. `--dry-run` valida argumentos, seleção,
anexos e argv sem iniciar a CLI. `--doctor --json` mostra apenas diagnóstico
operacional sanitizado; `--list-models --json` lista as combinações locais.

## Jobs em background

Quando a integração de jobs estiver instalada pela cápsula correspondente,
`--saida /caminho/privado.jsonl` inicia um job privado; `--status ID` consulta
somente o estado público e `--cancel ID` solicita cancelamento. A tarefa fica em
input temporário `0600`, o diretório de estado é privado `0700` e o input é
removido no estado terminal. `--status` sem tarefa não lê stdin.

Os jobs preservam o comportamento público `queued`, `running`, `completed`,
`failed`, `cancelled`, `timed_out` ou `lost`. O processo de controle do job recebe só o ID em argv; a CLI nativa recebe a tarefa por `-p`.
O prompt fica fora de metadata, status público e diagnóstico. A identidade usa PID, UID, starttime
e grupo do processo, além de `outputDev`/`outputIno`; o cancelamento usa
`pidfd` quando disponível. Em Linux, sem evidência suficiente de identidade, a
operação falha fechada. Metadata perdida pode perder a identidade do job e a
metadata de processo não carrega todo o conteúdo pedido.

O schema de `usage` preserva os campos nativos do provider, incluindo
`source: provider`; métricas reportadas são observabilidade e não orçamento de
tokens. Modelo e effort efetivos são conferidos no SQLite da sessão; a configuração global é comparada pelo `defaultModelSelection`.
Um hash do arquivo global inteiro pode variar sem mudança dessa seleção padrão.

## Conversa entre orquestrador e worker

O worker deixa de ser mudo durante a execução por três mecanismos opt-in:

1. **Streaming de eventos** — todo job grava `events.jsonl` (0600) no diretório
   privado com `turn_start`/`cli_output` (contagem de bytes por stream, sem
   conteúdo de tarefa ou resposta)/`turn_end`. O orquestrador acompanha o
   andamento em tempo real com `tail -f`.
2. **Protocolo de turnos** (`--conversation`) — o prompt recebe a instrução do
   protocolo e, quando falta decisão essencial, o worker encerra a resposta com
   `===NEED_INPUT===` seguido de `{"question": ..., "options": [...]}`. O
   wrapper devolve `status: "need_input"`, exit code **2**, `pending` com a
   pergunta e `turnResume` com a sessão; o orquestrador responde com
   `--resume <sessao>` na mesma sessão. Pedidos malformados viram erro
   `invalid_request` sem vazar conteúdo.
3. **Bridge MCP** (`bin/worker-bridge`) — servidor MCP stdio com
   `ask_orchestrator` (pergunta em `asks.jsonl`, resposta em `answer-<id>.json`,
   prazos e limites validados), `report_progress` e `request_stop`
   (`control.json`, idempotente). O job expõe seu id ao bridge via
   `ZCODE_WORKER_JOB_ID`; todos os arquivos seguem as guardas privadas do job.
   Registrar o servidor no ZCode é decisão do operador (a denylist headless
   não muda sozinha). Ao usá-lo: answers do orquestrador que embutirem
   conteúdo de terceiros (issue, e-mail) devem rotulá-lo como dado externo —
   o retorno da tool não passa pelo preâmbulo e o modelo é quem separa dado
   de instrução. Respostas não consumidas (`answer-*.json`) são removidas no
   estado terminal do job; `asks.jsonl`, `events.jsonl` e `control.json`
   permanecem como histórico.

O cancelamento agora é em dois estágios: SIGTERM ao líder identificado, espera
cooperativa de 2s e só então SIGKILL (o resultado público sinaliza
`killForced: true`). O sinal é enviado fora do lock do job para o filho poder
gravar seu estado terminal.

## Limitações e validação

stdin é uma entrada única e limitada a 96 KiB; a fila pode aguardar até o prazo
configurado. A implementação local não promete concorrência acima da trava
global da CLI. Retries só existem em `plan`, conforme descrito acima. Conteúdo
produzido pelo pedido pode aparecer na saída solicitada pelo usuário.

A suíte automatizada usa CLI fake, SQLite e processos reais locais isolados. A
cobertura exigida é 100% de linhas e branches em `cli.py`, `features.py` e
`jobs.py` (`scripts/validate.py coverage`); linhas defensivas comprovadamente
inatingíveis carregam `# pragma` comentado e o ramo `return->finally` do
cleanup de output, invisível ao tracer, é validado comportamentalmente. O
`tests/conftest.py` remove `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE` e
`ZCODE_DATA_BASE_DIR` herdados do ambiente (ex.: pytest dentro do app ZCode),
garantindo hermeticidade das fixtures. Sem `--queue-timeout`, a fila foreground
espera até 30s e um job `--saida` até 1800s; `--timeout` continua limitando
somente a execução. As provas nativas em `out/smoke-flash-low.json`,
`out/smoke-glm-high.json` e `out/smoke-default-candidate.json` registram
seleção efetiva conferida no SQLite e resultado real. O candidato padrão
Flash/high terminou com exit 0 e criou um arquivo novo. O relatório
`quality-gate-result.json` e a revisão independente são conferidos pelo
orquestrador antes da instalação. Cobertura e mutação dirigida não são prova
exaustiva. `src/zcode_worker/base.py` é snapshot somente de testes/staging;
produção injeta o core instalado atual e conserva suas guardas de memória e
locks — mantenha o snapshot sincronizado com o core instalado e regenere o
candidato com `scripts/make_candidate.py` após cada mudança do core.
