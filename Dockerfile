# Servico de visao do worker-ask (Coolify), com os CLIs OpenCode, Codex, Claude e ZCode.
FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 bash ca-certificates tini procps git \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g \
        @opencode/cli@2.3.0 \
        @openai/codex@0.169.0 \
        @anthropic-ai/claude-code@2.1.326 \
    && npm cache clean --force

RUN groupadd --gid 10001 worker \
    && useradd --uid 10001 --gid worker --home-dir /home/worker \
        --no-create-home --shell /bin/bash worker \
    && mkdir -p /home/worker \
    && chown worker:worker /home/worker

COPY worker_router.py availability.py quota_provider.py quota_reset.py worker_isolation.py /opt/worker-ask/
COPY bin/opencode-ask bin/claude-ask bin/codex-ask bin/ask_presentation.py /opt/worker-ask/bin/
COPY bin/zcode-cli bin/zcode-worker bin/zcode_headless_admit.py /opt/worker-ask/bin/
COPY adapters/zcode/auth/launch.cjs adapters/zcode/auth/zai-session.cjs /opt/worker-ask/adapters/zcode/auth/
COPY servico/vision_bridge.py servico/ordem.json servico/entrypoint.sh /opt/worker-ask/servico/

RUN ln -s /opt/worker-ask/worker_router.py /usr/local/bin/worker-ask \
    && chmod 755 /opt/worker-ask/worker_router.py \
        /opt/worker-ask/servico/vision_bridge.py \
        /opt/worker-ask/servico/entrypoint.sh \
        /opt/worker-ask/bin/opencode-ask \
        /opt/worker-ask/bin/claude-ask \
        /opt/worker-ask/bin/codex-ask \
        /opt/worker-ask/bin/ask_presentation.py \
        /opt/worker-ask/bin/zcode-cli \
        /opt/worker-ask/bin/zcode-worker \
        /opt/worker-ask/bin/zcode_headless_admit.py

# No Coolify, defina WAV_ALLOWED_HOSTS com o hostname pelo qual o cliente
# interno (phantoraCaptcha) chama este servico na rede Docker; sem ela so
# 127.0.0.1/localhost passam na checagem de Host e o cliente recebe 421.
ENV HOME=/home/worker \
    PATH=/opt/worker-ask/bin:$PATH \
    WORKER_ASK_ORDEM=/opt/worker-ask/servico/ordem.json \
    WAV_BIND=0.0.0.0 \
    WAV_ALLOW_ANY_BIND=1 \
    WAV_PORT=3000 \
    WAV_MAX_CONCURRENCY=1 \
    WAV_MAX_TIMEOUT=120 \
    WAV_NO_MUSE=1 \
    WAV_ORCHESTRATOR=claude \
    WAV_WORKER_ASK=worker-ask \
    ZCODE_ADMIT_MIN_MEM_MIB=1024 \
    ZCODE_WORKER_CONTEXT_CLI=/opt/worker-ask/bin/zcode-cli \
    PYTHONDONTWRITEBYTECODE=1

USER worker
WORKDIR /home/worker
EXPOSE 3000
VOLUME ["/home/worker"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3000/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini","--","/opt/worker-ask/servico/entrypoint.sh"]
CMD ["python3","/opt/worker-ask/servico/vision_bridge.py"]
