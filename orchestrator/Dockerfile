FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# docker CLI only (no local daemon) - DOCKER_HOST (docker-compose.yml) points
# it at the dind sidecar, the same isolated sandbox runner's agent sessions
# use, never the host's real docker. git_ops._force_remove is the only
# caller: a worktree teardown whose files were left root-owned by a
# container that ran as root inside dind (common default) needs root to
# actually remove, which this uid-1000 process doesn't have on its own.
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /state is a named volume (see docker-compose.yml) mounted at this exact
# path. Docker copies an empty volume's initial content - including
# ownership - from whatever's already at the mount point in the image, so
# this chown is what makes the volume writable by the uid:gid compose runs
# this service as (1000:1000, matching claudebox's aicode user), instead of
# defaulting to root and failing every write.
RUN mkdir -p /state && chown 1000:1000 /state

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090"]
