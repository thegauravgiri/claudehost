FROM node:22-bookworm-slim

# Install Python 3, Git, GitHub CLI, and developer tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    git \
    openssh-client \
    curl \
    ca-certificates \
    build-essential \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Docker *client* only (no daemon here) - talks to the "dind" sidecar over
# DOCKER_HOST (set in docker-compose.yml), so an agent session can run a
# project's own docker-compose.yml (its real PHP/Node/DB toolchain) for
# tests instead of needing every repo's stack pre-installed in this image.
# The daemon runs isolated in that sidecar, never the host's own Docker, so
# this container still can't reach claudehost's or the host's containers.
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
ARG CLAUDE_VERSION=latest
RUN npm install -g --no-audit --no-fund "@anthropic-ai/claude-code@${CLAUDE_VERSION}"

# node:22-bookworm-slim already ships a uid/gid-1000 "node" user, which would
# make a plain `useradd -u 1000` silently fail (and get swallowed by the
# claudebox-style `|| true` this used to have) - rename it instead of
# fighting over the uid, since docker-compose's volumes assume uid 1000.
RUN groupmod -n aicode node \
    && usermod -l aicode -d /home/aicode -m node

# Set up runner application
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .

# Ensure permissions
RUN mkdir -p /workspace /home/aicode/.claude \
    && chown -R 1000:1000 /workspace /home/aicode /app

USER aicode
ENV HOME=/home/aicode \
    CLAUDE_CONFIG_DIR=/home/aicode/.claude \
    WORKSPACE_ROOT=/workspace \
    PATH="/home/aicode/.local/bin:${PATH}"

EXPOSE 8080

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
