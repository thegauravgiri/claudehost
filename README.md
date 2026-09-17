# claudehost

**Turn a Claude Code subscription into an API.** claudehost converts your
Claude Pro/Max subscription (or an Anthropic API key) into a self-hosted,
OpenAI-compatible API endpoint: no per-token billing, no hand-built wrapper,
just Docker Compose and a shared gateway your whole team can call.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker Compose](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-agentic-D97757)](https://github.com/psyb0t/docker-claudebox)
[![LiteLLM](https://img.shields.io/badge/gateway-LiteLLM-6C5CE7)](https://docs.litellm.ai/)

Point any OpenAI SDK / client at the converted API and get both:

- **Chat completions**: ordinary prompt-in, text-out calls against Claude models.
- **Agentic coding tasks**: "run the tests and fix failures," "add this
  feature," "review this diff," executed for real against a mounted repo,
  with the same Claude Code bash/file-edit tools you get from the CLI.
- **Flat-rate cost control**: a whole team calls the gateway against one
  Claude Pro/Max subscription's fixed monthly price, instead of buying and
  metering an Anthropic API key whose bill scales with every token every
  developer uses.

Per-developer usage tracking, budgets, and virtual API keys are handled by
[LiteLLM](https://docs.litellm.ai/); the subscription-to-API conversion itself
is handled by a containerized [Claude Code](https://claude.com/claude-code)
session via [claudebox](https://github.com/psyb0t/docker-claudebox),
authenticated with your own Claude subscription or API key.

## Table of contents

- [Architecture](#architecture)
- [Available models and effort levels](#available-models-and-effort-levels)
- [Cost control: subscription vs. metered API](#cost-control-subscription-vs-metered-api)
- [How workspace routing works](#how-workspace-routing-works)
- [Security notes](#security-notes)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Configuration reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## Architecture

```
developer -> litellm :4000 (published, virtual keys, spend tracking)
               -> claudebox :8080 (internal only)
                    -> Claude Code session (subscription OAuth or API key)
```

Only `litellm`'s port is published. `claudebox` grants real bash/file access
via Claude Code, so it's reachable solely on the internal compose network.

## Available models and effort levels

Four models are configured, matching everything the pinned `claudebox`
adapter supports: `claude-haiku`, `claude-sonnet`, `claude-opus`, and
`claude-opusplan` (a hybrid mode: Opus plans, Sonnet executes).

Claude Code also supports a `--effort` level (`low`, `medium`, `high`,
`xhigh`, `max`) controlling how much it thinks before answering. The OpenAI
`reasoning_effort` request field is accepted by claudebox's API but not
wired to that flag in the pinned version, so it's silently ignored. The
working path is claudebox's generic CLI passthrough header instead:

```python
client.chat.completions.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "..."}],
    extra_headers={"X-Aicodebox-Extra-Args": '["--effort", "high"]'},
)
```

## Cost control: subscription vs. metered API

Buying an Anthropic API key means metered, pay-per-token billing: the bill
scales with every request every developer makes, and a busy week can spike
costs unpredictably. Routing the same traffic through a Claude Pro/Max
subscription instead gives you a single fixed monthly price for the whole
team, run through infrastructure you already control.

LiteLLM still tracks spend per virtual key for visibility, as shadow pricing:
each model's `pricing_ref` in [`config/litellm_config.yaml`](config/litellm_config.yaml)
names the real Anthropic model to price it like, and
[`docker/render_pricing.py`](docker/render_pricing.py) resolves that to actual
per-token rates from LiteLLM's own bundled cost map at image build time.
Nobody hand-types prices, and a rebuild picks up whatever rates ship with the
pinned `litellm` image.

The tradeoff is capacity, not cost: a subscription has its own usage caps and
rate limits, shared across everyone calling the gateway, and Anthropic's
consumer-plan terms are written around individual use rather than a team
reselling access to it through an API (see [Security notes](#security-notes)).
For a small-to-medium team's day-to-day usage this is usually a better deal
than metered billing; for high, bursty, or compliance-sensitive workloads,
set `ANTHROPIC_API_KEY` instead and pay per token.

## How workspace routing works

There's no separate "chat mode" vs "agent mode" toggle: every request goes
through the same Claude Code session. What changes is which workspace it's
pointed at, via the `X-Aicodebox-Workspace` request header:

- Omit it for plain Q&A/completions, no file/repo context.
- Set it to a project name (`./workspaces/<name>` on the host, `/workspace/<name>`
  in the container) for a session with real file/bash access to that checkout.

```python
client.chat.completions.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "run the test suite and fix any failures"}],
    extra_headers={"X-Aicodebox-Workspace": "my-service"},
)
```

This requires `forward_client_headers_to_llm_api: true` in
[`config/litellm_config.yaml`](config/litellm_config.yaml) (already set):
LiteLLM strips unrecognized headers by default.

## Security notes

- **Read [claudebox's source](https://github.com/psyb0t/docker-claudebox)
  before trusting it with your subscription token and shell access.** It runs
  Claude Code with `--permission-mode bypassPermissions` and passwordless
  sudo, so any request that reaches it can run arbitrary commands in whatever
  workspace it's pointed at. It's a small, single-maintainer project with no
  independent security audit; the source itself is clean (no telemetry, no
  obfuscation, no unexpected network calls), but treat it like any small OSS
  dependency, not a heavily-reviewed one.
- `CLAUDE_CODE_OAUTH_TOKEN` ties every developer's traffic to one Claude
  Pro/Max subscription's usage caps and rate limits, and Anthropic's
  consumer-plan terms are written around individual use, not a team reselling
  access to it through an API. That's a deliberate tradeoff for cost, not a
  compliance-cleared setup. Swap for `ANTHROPIC_API_KEY` (in `.env` and
  `docker-compose.yml`) if you need metered, ToS-clean billing instead;
  `claudebox` supports both.
- Put `litellm`'s published port behind TLS and restrict it to your VPN/office
  IP range before calling this production. The compose file itself does no
  network restriction beyond not publishing `claudebox`.

## Prerequisites

- Docker and Docker Compose
- A Claude Pro/Max subscription (for `claude setup-token`) or an Anthropic API key
- `openssl` (generating secrets)

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
   `http://<host>:4000/ui`, log in with username `admin` and your
   `LITELLM_MASTER_KEY` as the password), or via API:

   ```bash
   curl -X POST http://localhost:4000/key/generate \
     -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     -H "Content-Type: application/json" \
     -d '{"models": ["claude-haiku", "claude-sonnet", "claude-opus", "claude-opusplan"]}'
   ```

6. **Test**

   ```bash
   curl http://localhost:4000/v1/chat/completions \
     -H "Authorization: Bearer <virtual-key>" \
     -H "Content-Type: application/json" \
     -d '{"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]}'
   ```

   For an agentic run, put a repo under `./workspaces/<name>`, then call
   again with the header `X-Aicodebox-Workspace: <name>`.

## Configuration reference

| Variable | Used by | Purpose |
|---|---|---|
| `LITELLM_MASTER_KEY` | `litellm` | Root credential; mints/revokes virtual keys, also the admin UI password |
| `LITELLM_SALT_KEY` | `litellm` | Encrypts provider credentials LiteLLM stores in Postgres |
| `POSTGRES_PASSWORD` | `db`, `litellm` | Postgres auth for the virtual-key/spend-tracking database |
| `CLAUDEBOX_API_MODE_TOKEN` | `litellm`, `claudebox` | Shared bearer secret between the gateway and the Claude Code backend |
| `CLAUDE_CODE_OAUTH_TOKEN` | `claudebox` | Claude subscription auth, from `claude setup-token` |
| `CLAUDEBOX_GIT_NAME` / `CLAUDEBOX_GIT_EMAIL` | `claudebox` | Git identity for commits made inside agentic sessions |

## Troubleshooting

- **`claudebox` crash-loops with `ImportError: cannot import name
  'parse_native_event_lines'`**: a packaging bug in `psyb0t/claudebox:latest`
  and `:v2.4.2`. This repo pins `:v2.3.9`, which is confirmed working; don't
  bump the tag without testing it first.
- **`workspace` isn't reaching claudebox**: this pinned version only reads
  workspace selection from the `X-Aicodebox-Workspace` header, not a
  `workspace` body field. Use `extra_headers`, not `extra_body`.
- **claudebox fails to start with `mkdir: cannot create directory
  '/home/aicode': Permission denied`**: don't run it with `read_only: true`
  or `cap_drop: ALL`; its entrypoint needs to write to `/home/aicode` as root
  (via `CAP_DAC_OVERRIDE`) before dropping privileges.
- **`reasoning_effort` in a request has no effect**: the pinned claudebox
  adapter accepts but doesn't wire that field to Claude Code's `--effort`
  flag. Use the `X-Aicodebox-Extra-Args` header workaround instead (see
  [Available models and effort levels](#available-models-and-effort-levels)).

## Contributing

Issues and PRs welcome. This is a small, actively-used internal tool rather
than a large OSS project, so keep changes scoped and explain the "why."

## License

[MIT](LICENSE)
