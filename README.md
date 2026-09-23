# claudehost

Turn a Claude Code subscription into a self-hosted, OpenAI-compatible API. One Docker Compose stack gives your whole team a single endpoint for both normal chat completions and real agentic coding sessions, the same Claude Code you'd run from a terminal, callable over HTTP.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker Compose](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)](docker-compose.yml)

## What this is

Anthropic sells two different products: an API key (metered, pay per token) and a Claude Pro/Max subscription (flat monthly price, used through the `claude` CLI or claude.ai). This project lets a team share the subscription instead of the API key, behind a normal OpenAI-compatible endpoint, so any tool that already speaks the OpenAI API (a chatbot, an IDE plugin, a review agent) can use it without knowing the difference.

Two pieces do the work:

- **[LiteLLM](https://docs.litellm.ai/)**: the gateway. Issues per-developer API keys, tracks spend, exposes `/v1/chat/completions`.
- **[claudebox](https://github.com/psyb0t/docker-claudebox)**: runs the actual `claude` CLI in a container and exposes it as an OpenAI-compatible backend, authenticated with your subscription.

## Features

- **Chat completions**: plain prompt in, text out, same as calling the Anthropic API directly.
- **Agentic coding**: point a request at a real repo and Claude Code edits files, runs tests, and executes bash for real, not just in a text description.
- **Flat-rate cost control**: one subscription's fixed price for the whole team instead of a bill that scales with every token every developer sends.
- **Automatic concurrency handling**: stateless requests are isolated from each other automatically, so parallel traffic doesn't collide.
- **Self-updating cost tracking**: per-token pricing shown in LiteLLM's dashboard is pulled from LiteLLM's own bundled price list at build time, not hand-typed.

## Architecture

```
developer -> litellm :4000 (published)  -> claudebox :8080 (internal only) -> Claude Code -> Anthropic
              virtual keys, spend
```

Only `litellm`'s port is published. `claudebox` gets real shell access via Claude Code, so it's reachable only on the internal Docker network.

## Quick start

`claudebox`'s source is vendored as a git submodule; `git clone --recurse-submodules` pulls it in, or run `git submodule update --init` afterward if you already cloned without it. It's there for reference only, not needed to run the stack (`docker compose` pulls the built image from Docker Hub).

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

The tradeoff is capacity, not cost: a subscription has its own rate limits shared across everyone calling the gateway, and Anthropic's consumer-plan terms are written around individual use rather than a team reselling access through an API. That's a deliberate tradeoff for cost, not a compliance-cleared setup. For high-volume or compliance-sensitive workloads, set `ANTHROPIC_API_KEY` instead and pay per token; claudebox supports both.

## Security

- **Read claudebox's source before trusting it with your subscription token.** It's vendored as a git submodule at [`claudebox/`](claudebox) (upstream: [psyb0t/docker-claudebox](https://github.com/psyb0t/docker-claudebox)), so it's already in this repo, no separate clone needed. It runs Claude Code with `--permission-mode bypassPermissions` and passwordless sudo, so any request that reaches it can run arbitrary commands in whatever workspace it's pointed at. It's a small, single-maintainer project without an independent security audit; the source is clean (no telemetry, no obfuscation), but treat it like any small dependency, not a heavily-reviewed one.
- Put `litellm`'s published port behind TLS and restrict it to your VPN or office IP range before calling this production. The compose file itself does no network restriction beyond keeping `claudebox` off the public port.

## Configuration reference

| Variable | Used by | Purpose |
|---|---|---|
| `LITELLM_MASTER_KEY` | `litellm` | Root credential; mints/revokes virtual keys, also the admin UI password |
| `LITELLM_SALT_KEY` | `litellm` | Encrypts provider credentials LiteLLM stores in Postgres |
| `POSTGRES_PASSWORD` | `db`, `litellm` | Postgres auth for the virtual-key/spend-tracking database |
| `CLAUDEBOX_API_MODE_TOKEN` | `litellm`, `claudebox` | Shared bearer secret between the gateway and the Claude Code backend |
| `CLAUDE_CODE_OAUTH_TOKEN` | `claudebox` | Claude subscription auth, from `claude setup-token` |
| `CLAUDEBOX_GIT_NAME` / `CLAUDEBOX_GIT_EMAIL` | `claudebox` | Git identity for commits made inside agentic sessions (fed to git via `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env vars - the pinned claudebox version doesn't wire these into a gitconfig itself) |
| `ORCHESTRATOR_LITELLM_KEY` | `orchestrator` | Virtual key the orchestrator authenticates to `litellm` with |
| `GH_TOKEN` | `claudebox`, `orchestrator` | Fine-grained GitHub PAT: git push/pull and `gh pr create` for the Jira agent flow |
| `GH_WEBHOOK_SECRET` | `orchestrator` | Verifies inbound GitHub webhooks (PR merged/closed) |
| `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` | `orchestrator` | Jira Cloud REST API auth for reading `/agent` comments and posting results |
| `JIRA_WEBHOOK_SECRET` | `orchestrator` | Verifies inbound Jira webhooks |

Git access for agentic sessions that need to `git clone`/push to a private repo:

```bash
mkdir -p ssh
ssh-keygen -t ed25519 -f ssh/claudebox -N ""
```

Add `ssh/claudebox.pub` as a deploy key on your git host. `ssh/` mounts as a directory, so the private key lands at `/home/aicode/.ssh/claudebox/claudebox` inside the container, not somewhere SSH checks by default; `GIT_SSH_COMMAND` in `docker-compose.yml` already points at that exact path.

A deploy key only ever attaches to one repository, so it's a fine default for a single-project agentic workspace but not for the Jira flow below, which needs one credential across many repos.

## Jira-driven coding agent

The `orchestrator` service turns Claude Code into a coding agent you talk to inside a Jira issue. Comment `/agent go` and it implements the issue in a real checkout, verifies its own work, opens a PR, and reports back - then comment `/agent also handle the edge case where X` and it picks up the same branch and PR rather than starting over. It's optional; the base stack works without it.

How it stays possible for three issues on the same repo to run at once: each issue gets its own [git worktree](https://git-scm.com/docs/git-worktree) checked out from one shared bare clone per repo, so file writes never collide and git itself refuses to check out the same branch twice.

Setup:

1. Switch `claudebox`'s image to the `-full` variant (already done in `docker-compose.yml`) - the flow needs `gh` and language toolchains preinstalled, since `security_opt: no-new-privileges` blocks the minimal image's usual "install what you need via sudo" fallback.
2. Fill in the Jira/GitHub block in `.env` (see `.env.example`) - a Jira Cloud API token, a fine-grained GitHub PAT scoped to `Contents` and `Pull requests` on the repos you want it to touch, and two webhook secrets (`generate_secrets.sh` can generate those two).
3. List your repos in `orchestrator/repos.yaml`: Jira project key → GitHub remote + default branch.
4. Point a Jira Automation rule (comment created/updated) at the orchestrator's webhook endpoint, and a GitHub webhook (pull request closed) at the same service, using the two secrets from step 2.

`workspaces/CLAUDE.md` and `.init-done` live on claudebox's persistent volume and are only generated on first container create - if you're switching an existing deployment to the `-full` image, delete both so they regenerate with the full toolchain list; otherwise the agent is told it's on the minimal image and it isn't.

## License

[MIT](LICENSE)
