# claudehost: advanced topics

Everything that doesn't fit in the main [README](../README.md) quick start:
architecture details, cost internals, scaling past one shared subscription,
full security notes, config reference, and troubleshooting.

## Table of contents

- [Available models and effort levels](#available-models-and-effort-levels)
- [Cost control: subscription vs. metered API](#cost-control-subscription-vs-metered-api)
- [How workspace routing works](#how-workspace-routing-works)
  - [Concurrency: why omitting the header is now safe](#concurrency-why-omitting-the-header-is-now-safe)
  - [Git access for agentic sessions](#git-access-for-agentic-sessions)
- [Per-developer instances](#per-developer-instances)
- [Security notes](#security-notes)
- [Configuration reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)

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
each model's `pricing_ref` in [`config/litellm_config.yaml`](../config/litellm_config.yaml)
names the real Anthropic model to price it like, and
[`docker/render_pricing.py`](../docker/render_pricing.py) resolves that to actual
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

- Set it to a project name (`./workspaces/<name>` on the host, `/workspace/<name>`
  in the container) for a session with real file/bash access to that checkout.
- Omit it for plain Q&A/completions with no file/repo context - see below for
  what actually happens when you do.

```python
client.chat.completions.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "run the test suite and fix any failures"}],
    extra_headers={"X-Aicodebox-Workspace": "my-service"},
)
```

This requires `forward_client_headers_to_llm_api: true` in
[`config/litellm_config.yaml`](../config/litellm_config.yaml) (already set):
LiteLLM strips unrecognized headers by default.

### Concurrency: why omitting the header is now safe

claudebox allows only one active Claude Code process per workspace, and
rejects (doesn't queue) a concurrent request to the same one with `409
workspace busy` (see [Troubleshooting](#troubleshooting)). Every request
that omits `X-Aicodebox-Workspace` used to land on the same default
workspace, so concurrent stateless traffic (a chatbot, a code-review agent
firing off parallel calls) would collide and start failing under load.

[`docker/auto_workspace_callback.py`](../docker/auto_workspace_callback.py)
fixes this at the gateway: a LiteLLM pre-call hook that assigns a random,
throwaway workspace to any request that doesn't already specify one, so
purely stateless callers get automatic isolation with zero client-side
changes. Requests that *do* set `X-Aicodebox-Workspace` themselves (real
agentic sessions against a real project) are left untouched - the hook
only acts when the header is absent. It's registered via
`litellm_settings.callbacks` in
[`config/litellm_config.yaml`](../config/litellm_config.yaml).

The same hook also cleans up after itself: it deletes the `auto-<uuid>`
workspace directory right after each call finishes (success or failure),
so nothing accumulates on disk. This needs `litellm` to share the
`./workspaces` mount with `claudebox` (see `docker-compose.yml`) - if you
ever see `auto-*` directories piling up, check that mount is still in
place before assuming a bug in the hook itself. Cleanup only ever touches
directories it created (the `auto-` prefix); anything you named yourself
is never removed.

### Git access for agentic sessions

Only needed if an agentic session should `git clone`/push to a private repo:

```bash
mkdir -p ssh
ssh-keygen -t ed25519 -f ssh/claudebox -N ""
```

Add `ssh/claudebox.pub` as a deploy key (or bot account key) on your git
host. `ssh/` is already bind-mounted read-only into `claudebox`.

## Per-developer instances

The default setup shares one `claudebox` (one subscription, one rate limit)
across everyone. For a team of ~10-15, give each person their own instance
instead, so their traffic runs against their own subscription:

1. `cp developers.json.example developers.json` and list your team (name,
   slug, git email). `developers.json` is gitignored, since it's your
   team's real names/emails, not a secret but not template content either.
2. Add one OAuth token per developer to `.env`:
   `CLAUDE_CODE_OAUTH_TOKEN_<SLUG>=sk-ant-oat01-...` (from that person's own
   `claude setup-token`).
3. `python3 docker/generate_developers.py`. This (re)writes
   `docker-compose.developers.yml` (one `claudebox-<slug>` service, own auth
   volume, per developer) and `config/litellm_config.developers.yaml` (one
   `<tier>-<slug>` model per Claude tier per developer), from
   `docker/generate_developers.py`'s templates, not hand-edited.
4. `docker compose -f docker-compose.yml -f docker-compose.developers.yml up -d --build`.
5. Scope each developer's virtual key to only their own models
   (`"models": ["sonnet-alice", "haiku-alice", ...]`), so nobody can spend
   against someone else's subscription.

Both generated files will contain your team's real names/emails once
populated; if you don't want that in this repo's git history, gitignore
them yourself before running the generator for real. The default
(unpopulated) versions are committed as empty placeholders so a fresh clone
still builds without running the generator first.

`mem_limit`/`cpus` per developer default lower (2g/1 CPU) than the shared
instance, since these are limits, not reservations. Size your host for a
few people being busy at once, not the whole roster simultaneously.

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

- **`409 workspace busy, retry later`** under concurrent load: fixed by the
  auto-workspace pre-call hook for stateless traffic - see
  [Concurrency: why omitting the header is now safe](#concurrency-why-omitting-the-header-is-now-safe).
  If you're still seeing it, you're likely sending an explicit
  `X-Aicodebox-Workspace` value yourself and firing concurrent requests at
  that same value - give each concurrent, unrelated task its own name.
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
