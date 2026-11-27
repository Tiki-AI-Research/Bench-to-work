# Isolated Worker execution container for Bench-to-Work tasks:
# - Installs Codex CLI and Claude Code CLI behind /opt/worker.py (JSON stdio command protocol).
# - Installs Office & PDF libraries in /opt/worker-env.
# - Mounts read-only public preview hooks at /opt/task-public (root-owned; hidden rubrics and future
#   materials remain on the host controller and never enter the build context).
FROM node:22-bookworm-slim
ARG DEBIAN_MIRROR=http://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=http://deb.debian.org/debian-security
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG CODEX_VERSION=0.154.0
ARG CLAUDE_CODE_VERSION=2.1.303
RUN sed -i \
      -e "s|https\?://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
      -e "s|https\?://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv git ripgrep ca-certificates procps \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g --registry="${NPM_REGISTRY}" "@openai/codex@${CODEX_VERSION}" "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force
COPY requirements.txt /opt/requirements.txt
RUN python3 -m venv /opt/worker-env \
    && /opt/worker-env/bin/pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r /opt/requirements.txt \
    && printf '%s\n' 'export PATH=/opt/worker-env/bin:$PATH' > /etc/profile.d/worker-env.sh
ENV PATH="/opt/worker-env/bin:${PATH}" PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1
COPY worker.py check_env.py /opt/
COPY public/ /opt/task-public/
# Clear sticky bit on /tmp so non-root UID 1000 can clean up per-call staged files uploaded via docker cp.
RUN chmod -R a+rX,go-w /opt/task-public /opt/worker.py /opt/check_env.py \
    && mkdir -p /workspace && chown 1000:1000 /workspace \
    && chmod 0777 /tmp
USER 1000:1000
WORKDIR /workspace
RUN bash -lc 'python3 /opt/check_env.py' && codex --version && claude --version
ENTRYPOINT []
CMD ["sleep", "infinity"]
