# Escopo exclusivo: worker-ask

- Este repositório mantém o roteador, todos os wrappers -ask, seus auxiliares e as integrações ZCode/Bonsai. Não alterar iptzao nem outros projetos nesta tarefa.
- PT-BR, patches pequenos, ler o código antes de editar. Não reverter trabalho alheio.
- Delegação comum somente por `worker-ask --orchestrator <plataforma-real>`, com escopo explícito e `--allowed-file` em escrita. Workers não fazem commit nem delegam.
- Fallback não pode ser ampliado por mensagem de erro: rc1 para; rc75 exige conferir os arquivos autorizados. Stderr bruto permanece privado. Única exceção, decidida por estado e nunca por texto: com isolamento e `WORKER_ASK_STATE_FALLBACK=1`, executor sem shell (ZCode, Muse, OpenCode) que falha com rc1/timeout deixando a cópia e os arquivos autorizados intactos deixa a cadeia seguir (`_iso_no_effect`). Não ampliar essa lista de executores nem afrouxar as condições sem teste de mutação.
- Isolamento (`worker_isolation.py`): o módulo não importa o roteador (recebe as funções por `Deps`); na árvore real só escreve em `sync_back` e só em arquivo autorizado; `reset`/`clean`/`rmtree` só depois da guarda do slot. Padrão desligado.
- Nunca versionar credenciais, login, sessões, bancos, provider configs reais ou bundles proprietários. Ler `~/.zcode/docs/SEGURANCA.md` para auth e revisão de segurança.
- Antes de declarar pronto ou commitar: revisar diff, executar testes relevantes e verificar os nove blocos do quality gate. Não substituir falha por N/A sem motivo.
- Commit e push somente com pedido explícito; não fazer force push, merge ou reset destrutivo. Nesta criação, o usuário autorizou commit e push para um repositório próprio.
- A raiz é a fonte de verdade. Fontes locais vinculadas podem afetar o comando instalado imediatamente. Preservar backups e compatibilidade do core regular instalado.

- Instalador altera somente código nos destinos declarados. Não ler ou copiar credenciais, sessões, catálogos de conta ou modelos. Preservar backups e testar rollback/idempotência em HOME temporária antes de ativar.
- O revisor local do Git pode bloquear importações grandes. Não mudar sua configuração, reescrever/publicar outra história ou usar REVISOR_PULAR sem escolha explícita do usuário.

- Workers sao independentes da plataforma do orquestrador: Codex pode usar Luna via roteador. Nao excluir plataforma inteira; impedir delegacao recursiva com `WORKER_ASK_ACTIVE`. Esta regra atende ao pedido explicito do usuario e prevalece sobre a exclusao antiga da home.

- Cadeia vigente: ZCode → Muse (padrao; opt-out por projeto via .worker-ask-no-muse/--no-muse) → OpenCode → Codex Luna → Claude Haiku. A remocao do degrau Terra que ja existia no working tree foi preservada; a cadeia atual substitui a intencao antiga do commit 07cb87c. O handler codex-terra permanece apenas por compatibilidade, sem participar do roteamento.
