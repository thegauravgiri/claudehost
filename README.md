# claudehost

Exposes Claude Code (running under a Claude Pro/Max subscription) to developers
through a single OpenAI-compatible LiteLLM gateway — supporting both plain
chat completions and full agentic coding sessions (bash/file tools against a
real repo checkout).

## Architecture

```
developer -> litellm :4000 (published, virtual keys, spend tracking)
               -> claudebox :8080 (internal only)
                    -> Claude Code session (subscription OAuth auth)
```

`claudebox` ([psyb0t/docker-claudebox](https://github.com/psyb0t/docker-claudebox))
is a third-party open-source image that runs Claude Code and exposes it over
several interfaces, including an OpenAI-compatible `/openai/v1/chat/completions`
adapter. LiteLLM is configured to treat it as a generic OpenAI-compatible
backend (`config/litellm_config.yaml`). This avoids hand-building an API
wrapper around Claude Code — `claudebox` already is one.

**Read its source before trusting it with your subscription token and shell
access.** It runs Claude Code with `--permission-mode bypassPermissions` and
passwordless sudo inside the container — i.e. any request that reaches it can
run arbitrary commands in whatever workspace it's pointed at. That's why its
port is never published to the host, only reachable from the `litellm`
service on the internal compose network.

There is no separate "chat mode" vs "agent mode" toggle. Every request goes
through the same Claude Code session; what changes is which workspace it's
pointed at:

- Omit `workspace` (or point it at an empty directory) for plain Q&A/completions.
- Set `workspace` to a project name (resolves to `/workspace/<name>` inside
  the container, i.e. `./workspaces/<name>` on the host) to get a session with
  real file/bash access to that checkout.

Pass it from an OpenAI SDK client via `extra_body`:

```python
client.chat.completions.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "run the test suite and fix any failures"}],
    extra_body={"workspace": "my-service"},
)
```

**Verify this on first deploy** — LiteLLM's pass-through of non-standard body
fields to a custom OpenAI-compatible backend hasn't been tested against this
specific pairing; confirm `workspace` actually reaches claudebox before
relying on it (check `claudebox`'s `/status` endpoint or its logs).

## Before you deploy: the subscription-sharing tradeoff

`CLAUDE_CODE_OAUTH_TOKEN` ties every developer's traffic to one Claude
Pro/Max subscription's usage caps and rate limits — heavy use by one person
throttles everyone. Anthropic's consumer-plan terms are written around
individual use, not a team reselling access to it through an API; treat this
as a deliberate tradeoff you're accepting for cost, not a compliance-cleared
setup. Swap `CLAUDE_CODE_OAUTH_TOKEN` for `ANTHROPIC_API_KEY` in both
`.env` and `docker-compose.yml` if you need metered, ToS-clean billing instead
— `claudebox` supports both.

## Setup

1. **Generate secrets**

   ```bash
   cp .env.example .env
   openssl rand -base64 32   # -> LITELLM_MASTER_KEY (prefix with sk-)
   openssl rand -base64 32   # -> LITELLM_SALT_KEY (prefix with sk-)
   openssl rand -hex 32      # -> CLAUDEBOX_API_MODE_TOKEN
   openssl rand -base64 24   # -> POSTGRES_PASSWORD
   ```

   Fill these into `.env`.

2. **Mint the Claude Code OAuth token** on any machine with the `claude` CLI
   installed and logged into the subscription you want to share:

   ```bash
   claude setup-token
   ```

   Paste the resulting `sk-ant-oat01-...` token into `.env` as
   `CLAUDE_CODE_OAUTH_TOKEN`.

3. **Git access for the agent** (only needed if agentic sessions will clone
   or push to private repos):

   ```bash
   mkdir -p ssh
   ssh-keygen -t ed25519 -f ssh/claudebox -N ""
   ```

   Add `ssh/claudebox.pub` as a deploy key (or bot account key) on your git
   host. `ssh/` is bind-mounted read-only into the container.

4. **Boot the stack**

   ```bash
   docker compose up -d --build
   ```

5. **Create a virtual key** for a developer/team (LiteLLM UI at
   `http://<host>:4000/ui`, log in with `LITELLM_MASTER_KEY`), or via API:

   ```bash
   curl -X POST http://localhost:4000/key/generate \
     -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     -H "Content-Type: application/json" \
     -d '{"models": ["claude-haiku", "claude-sonnet", "claude-opus"]}'
   ```

6. **Test**

   ```bash
   curl http://localhost:4000/v1/chat/completions \
     -H "Authorization: Bearer <virtual-key>" \
     -H "Content-Type: application/json" \
     -d '{"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]}'
   ```

   For an agentic run, first put a repo under `./workspaces/<name>`, then
   call again with `extra_body: {"workspace": "<name>"}`.

## Operational notes

- Put `litellm`'s published port behind TLS and restrict it to your VPN/office
  IP range before calling this "production" — the compose file itself does
  no network restriction beyond not publishing `claudebox`.
- `claudebox` runs with `read_only: true` + a `tmpfs` `/tmp` per the upstream
  project's own hardening recommendation. If a `claudebox` image update needs
  another writable path, you'll see it fail at startup — add a targeted
  volume/tmpfs rather than removing `read_only`.
- Workspaces live at `./workspaces/<name>` on the host — back this up like
  you would any other working repo checkout.
