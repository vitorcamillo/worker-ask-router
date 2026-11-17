# Login dos CLIs do serviço de visão

## O que é

Serviço HTTP de visão do `worker-ask`, acessível somente pela rede interna do
Docker na porta 3000. O phantoraCaptcha chama `POST /v1/vision` com Bearer
token; `GET /healthz?clis=1` mostra quais CLIs estão logados.

## Como entrar

Conecte no servidor pela tailnet e abra um shell no container como `worker`:

```bash
docker exec -it -u worker $(docker ps -qf name=<uuid-do-app> | head -1) bash
```

`<uuid-do-app>` é o placeholder literal do identificador do app no Coolify.

## Login por CLI

### OpenCode

```bash
opencode auth login
```

Escolha o provedor do plano (OpenCode Go/Zen); abre uma URL com código para
colar no navegador. Confira com `opencode auth list`.

### Codex

```bash
codex login --device-auth
```

Abre URL e código de dispositivo. Confira com `codex login status`.

### Claude Code

```bash
claude auth login
```

Também funciona com `claude` e `/login`: abre uma URL; cole o código de volta.
Confira com `claude auth status`.

### ZCode

Pré-requisito: bundle do ZCode em `~/.local/share/zcode-cli/glm/` (`zcode.cjs`,
`provider/zcode-builtin.json`, `VERSION`), transferido pelo operador para o
volume `/home/worker`; nunca é versionado nem embutido na imagem.

Login por polling (sem callback nem navegador local), dentro do container como
`worker`:

```bash
ZCODE_DATA_BASE_DIR=~/.local/state/zcode-worker/auth zcode-cli login zai --no-browser
```

O comando imprime `Open this URL to sign in:` e uma URL
`https://chat.z.ai/api/oauth/authorize?...`; abra no navegador do seu celular ou
PC, autorize com a conta do plano e o comando conclui sozinho.

A env `ZCODE_CREDENTIAL_SECRET_FILE` já vem do entrypoint; num `docker exec` novo,
exporte antes:

```bash
export ZCODE_CREDENTIAL_SECRET_FILE=~/.config/zcode-worker/credential-secret
```

Confira em `GET /healthz?clis=1`; o check `zcode` é o da fatia anterior, se
existir — caso contrário, rode `worker-ask --status`.

## Depois

```bash
curl -s localhost:3000/healthz?clis=1
```

A resposta deve mostrar `"logado"` para cada CLI autenticado.

Credenciais ficam no volume de `/home/worker` (sobrevivem a deploy/restart);
nunca copiar credencial de outra máquina. Muse não está na imagem (privacidade,
conteúdo pode ser usado pelo provedor); ZCode entra sem bundle versionado, só com
o bundle que o operador transfere para o volume.
