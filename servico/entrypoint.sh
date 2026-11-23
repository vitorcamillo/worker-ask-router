#!/usr/bin/env bash
set -euo pipefail

umask 077

mkdir -p \
    "$HOME/.local/bin" \
    "$HOME/.local/share/zcode-worker/auth" \
    "$HOME/.local/share/zcode-cli/glm/provider" \
    "$HOME/.local/state/zcode-worker/auth" \
    "$HOME/.config/zcode-worker"

ln -sfn /opt/worker-ask/bin/zcode_headless_admit.py "$HOME/.local/bin/zcode_headless_admit.py"
ln -sfn /opt/worker-ask/adapters/zcode/auth/launch.cjs "$HOME/.local/share/zcode-worker/auth/launch.cjs"
ln -sfn /opt/worker-ask/adapters/zcode/auth/zai-session.cjs "$HOME/.local/share/zcode-worker/auth/zai-session.cjs"
ln -sfn /opt/worker-ask/bin/zcode-cli "$HOME/.local/bin/zcode-cli"

SECRET_FILE="$HOME/.config/zcode-worker/credential-secret"
if [ ! -e "$SECRET_FILE" ]; then
    head -c 48 /dev/urandom > "$SECRET_FILE"
fi
export ZCODE_CREDENTIAL_SECRET_FILE="$SECRET_FILE"

# ZCODE_APP_VERSION nao e fixado aqui: o bin/zcode-cli le glm/VERSION a cada chamada,
# entao a versao acompanha o auto-update abaixo sem reiniciar o container.

# Auto-update diario do bundle ZCode (feed oficial) e dos CLIs npm (opencode, codex,
# claude) no volume. Cada script troca os arquivos atomicamente e, se falhar, deixa a
# versao atual intacta. WORKER_AUTO_UPDATE=0 desliga.
if [ "${WORKER_AUTO_UPDATE:-1}" != "0" ]; then
    update_delay="${WORKER_UPDATE_DELAY:-60}"
    case "$update_delay" in
        ''|*[!0-9]*) echo "entrypoint: WORKER_UPDATE_DELAY invalido ($update_delay); usando 60" >&2; update_delay=60 ;;
    esac
    update_interval="${WORKER_UPDATE_INTERVAL:-86400}"
    case "$update_interval" in
        ''|*[!0-9]*) echo "entrypoint: WORKER_UPDATE_INTERVAL invalido ($update_interval); usando 86400" >&2; update_interval=86400 ;;
    esac
    (
        sleep "$update_delay"
        while :; do
            timeout 1800 /opt/worker-ask/bin/zcode-update || true
            timeout 1800 /opt/worker-ask/bin/cli-update || true
            sleep "$update_interval"
        done
    ) </dev/null &
fi

exec "$@"
