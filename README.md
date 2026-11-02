# claudehost

Turn a Claude Code subscription into a self-hosted, OpenAI-compatible API. One Docker Compose stack gives your whole team a single endpoint for both normal chat completions and real agentic coding sessions, the same Claude Code you'd run from a terminal, callable over HTTP.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker Compose](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)](docker-compose.yml)

## What this is

Anthropic sells two different products: an API key (metered, pay per token) and a Claude Pro/Max subscription (flat monthly price, used through the `claude` CLI or claude.ai). This project lets a team share the subscription instead of the API key, behind a normal OpenAI-compatible endpoint, so any tool that already speaks the OpenAI API (a chatbot, an IDE plugin, a review agent) can use it without knowing the difference.

Two pieces do the work:

- **[LiteLLM](https://docs.litellm.ai/)**: the gateway. Issues per-developer API keys, tracks spend, exposes `/v1/chat/completions`.
- **runner** (`runner/`): runs the actual `claude` CLI in a container and exposes it as an OpenAI-compatible backend, authenticated with your subscription. It replaces the earlier claudebox image.

## Features

- **Chat completions**: plain prompt in, text out, same as calling the Anthropic API directly.
- **Agentic coding**: point a request at a real repo and Claude Code edits files, runs tests, and executes bash for real, not just in a text description.
- **Flat-rate cost control**: one subscription's fixed price for the whole team instead of a bill that scales with every token every developer sends.
- **Automatic concurrency handling**: stateless requests are isolated from each other automatically and queued (never dropped) when many arrive at once. Identical in-flight requests share one run.
- **Multiple subscriptions**: the model name picks which Claude account serves a request (see [Multiple Claude accounts](#multiple-claude-accounts)).
- **Lower usage**: stateless conversations resume the same Claude session so earlier turns hit the prompt cache, OpenAI tool calling works, output is capped, and abandoned requests are cancelled. A session limit returns HTTP 429 instead of being logged as a success.
- **Self-updating cost tracking**: per-token pricing shown in LiteLLM's dashboard is pulled from LiteLLM's own bundled price list at build time, not hand-typed.

## Architecture

```
developer -> litellm :4000 (published)  -> runner :8080 (internal only) -> Claude Code -> Anthropic
              virtual keys, spend
```

Only `litellm`'s port is published. `runner` gets real shell access via Claude Code, so it's reachable only on the internal Docker network.

## Quick start

The `claudebox/` submodule is a leftover from the previous backend and is not used by this stack; you can ignore it.

```bash
cp .env.example .env
```

Fill in `.env`:

- Four secrets, generated with `openssl rand -base64 32` / `openssl rand -hex 32` (see the comments next to each variable for which command fits)
- `CLAUDE_CODE_OAUTH_TOKEN`: run `claude setup-token` on a machine logged into the subscription you want to share, and paste the result

Then:

```bash
docker compose up -d --build
```

Mint a key and test it:

```bash
curl -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"models": ["claude-haiku", "claude-sonnet", "claude-opus", "claude-opusplan"]}'

curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer <virtual-key-from-above>" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]}'
```

Prefer not to do this by hand? Run the `setup-claudehost` skill: it does all of the above conversationally and proves the whole chain works with a real request.

## Using it as a coding agent

A plain chat completion has no file access. To get an agent that can actually read, edit, and run code, check a repo out under `./workspaces/<name>` and add one header:

```python
client.chat.completions.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "run the test suite and fix any failures"}],
    extra_headers={"X-Aicodebox-Workspace": "my-service"},
)
```

Same endpoint, same model: the header is what turns a chatbot into an agent working on your actual code.

## Cost control

An Anthropic API key bills per token: the cost scales with every request from every developer, and a busy week can spike unpredictably. A subscription is a flat monthly price for the whole team instead.

LiteLLM still tracks spend per key for visibility. Each model's `pricing_ref` in `litellm/config.yaml` names the real Anthropic model to price it like, and `litellm/render_pricing.py` resolves that to actual per-token rates from LiteLLM's own bundled cost map at build time. Nothing is billed against these numbers; they're shown purely so you can see who's using how much.

The tradeoff is capacity, not cost: a subscription has its own rate limits shared across everyone calling the gateway, and Anthropic's consumer-plan terms are written around individual use rather than a team reselling access through an API. That's a deliberate tradeoff for cost, not a compliance-cleared setup. For high-volume or compliance-sensitive workloads, set `ANTHROPIC_API_KEY` instead and pay per token; the `claude` CLI supports both.

## Security

- **`runner` runs Claude Code with `--permission-mode bypassPermissions`**, so any request that reaches it can run arbitrary commands in whatever workspace it's pointed at. Keep it off public networks (only `litellm` is published) and treat every key that can call the gateway as having that power.
- Put `litellm`'s published port behind TLS and restrict it to your VPN or office IP range before calling this production. The compose file itself does no network restriction beyond keeping `runner` off the public port.

## Configuration reference

| Variable | Used by | Purpose |
|---|---|---|
| `LITELLM_MASTER_KEY` | `litellm` | Root credential; mints/revokes virtual keys, also the admin UI password |
| `LITELLM_SALT_KEY` | `litellm` | Encrypts provider credentials LiteLLM stores in Postgres |
| `POSTGRES_PASSWORD` | `db`, `litellm` | Postgres auth for the virtual-key/spend-tracking database |
| `CLAUDEBOX_API_MODE_TOKEN` | `litellm`, `runner` | Shared bearer secret between the gateway and the Claude Code backend |
| `CLAUDE_CODE_OAUTH_TOKEN` | `runner` | Default Claude subscription, from `claude setup-token` |
| `CLAUDE_OAUTH_TOKEN_<NAME>` | `runner` | Extra subscription named `<name>`; put these in `claude-accounts.env` |
| `CLAUDEBOX_GIT_NAME` / `CLAUDEBOX_GIT_EMAIL` | `runner` | Git identity for commits made inside agentic sessions |

Git access for agentic sessions that need to `git clone`/push to a private repo:

```bash
mkdir -p ssh
ssh-keygen -t ed25519 -f ssh/claudebox -N ""
```

Add `ssh/claudebox.pub` as a deploy key on your git host. `ssh/` mounts as a directory, so the private key lands at `/home/aicode/.ssh/claudebox/claudebox` inside the container, not somewhere SSH checks by default; `GIT_SSH_COMMAND` in `docker-compose.yml` already points at that exact path.

## Multiple Claude accounts

`CLAUDE_CODE_OAUTH_TOKEN` is the `default` account, used by every model that doesn't say otherwise, so existing model names keep working. To add more subscriptions:

1. Copy `claude-accounts.env.example` to `claude-accounts.env` (git-ignored) and add one line per account: `CLAUDE_OAUTH_TOKEN_MCM=sk-ant-oat01-...` creates the account `mcm`. Generate each token with `claude setup-token`.
2. Run `docker compose up -d runner`. `http://runner:8080/healthz` (from inside the network) lists the loaded accounts.
3. In the LiteLLM admin UI go to **Models > Add Model**, choose an OpenAI-compatible provider, and set:
   - **Public model name**: any prefix you like, e.g. `mcm-claude-sonnet`
   - **Model**: `openai/sonnet` (or `haiku`, `opus`)
   - **API Base**: `http://runner:8080/openai/v1`
   - **API Key**: the value of `CLAUDEBOX_API_MODE_TOKEN`
   - **Extra params (JSON)**: `{"extra_headers": {"X-Aicodebox-Account": "mcm"}, "num_retries": 0}`

Calling `mcm-claude-sonnet` then uses the `mcm` subscription. An unknown account name returns HTTP 400. Each account has its own queue and session-limit state, so one account hitting its limit never blocks another. Conversation resume and request merging never cross accounts. Tokens stay in the runner's environment, not in LiteLLM's database.

## Tuning

All optional environment variables on the `runner` service:

| Variable | Default | Meaning |
|---|---|---|
| `MAX_CONCURRENT_STATELESS` | `6` | Parallel `claude` processes per account for stateless calls; extra requests wait in a queue |
| `MAX_CONCURRENT_AGENT` | `4` | Same, for calls that set a workspace |
| `STATELESS_QUEUE_TIMEOUT_SECONDS` | `240` | Max wait for a stateless slot before HTTP 503 |
| `STATELESS_TIMEOUT_SECONDS` | `240` | Hard deadline for a stateless call (HTTP 504) |
| `STATELESS_MAX_OUTPUT_TOKENS` | `32000` | Output ceiling for stateless calls (the caller's `max_tokens` can only lower it) |
| `CONVERSATION_TTL_SECONDS` | `3600` | How long a stateless conversation stays resumable; `0` disables resume |

## License

[MIT](LICENSE)
