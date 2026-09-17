---
name: setup-claudehost
description: Set up this repo's claudehost stack (LiteLLM gateway + claudebox running Claude Code) end to end, from a fresh clone to a verified-working API endpoint. Use this whenever the user wants to set up, install, bootstrap, deploy, or get running with this project for the first time - phrases like "set this up", "get this running", "how do I start this", "deploy this to the server", or "run the setup" all mean this skill, even if they don't name it. Also use it if .env is missing or incomplete and the user just tried `docker compose up`. Handles secret generation, collects the Claude Code OAuth token conversationally, boots the stack, and proves it actually works with a real request - not just that containers show "Up".
---

# Setting up claudehost

This walks a user from a fresh clone of this repo to a verified-working
LiteLLM + Claude Code API endpoint. The steps mirror README.md's own
"Setup" section, automated and made idempotent so re-running this after a
partial setup doesn't clobber anything already configured.

Work from the repo root throughout (the directory containing
`docker-compose.yml`).

## 1. Check prerequisites

```bash
docker --version && docker compose version
```

If either is missing, stop here and tell the user to install Docker
Desktop (or Docker Engine + the compose plugin on Linux) before continuing
- everything downstream depends on it.

## 2. Set up .env without clobbering existing values

If `.env` doesn't exist yet: `cp .env.example .env`.

If it already exists, don't overwrite it blindly - someone may have
already partially configured it (including a previous run of this skill).
Instead, read it and find which required secrets still hold the literal
placeholder text from `.env.example` (every one of them contains the
string `CHANGE-ME`):

```bash
grep CHANGE-ME .env || echo "no placeholders left"
```

Only the variables that still say `CHANGE-ME` need attention below. If
`.env` already looks fully configured, skip straight to step 5 after
confirming with the user that they want to proceed with the existing
values (don't silently reuse someone else's guess about what they meant).

## 3. Get the Claude Code OAuth token

If `CLAUDE_CODE_OAUTH_TOKEN` still has its placeholder, ask the user
directly in the conversation to paste their token. This can't be an
`AskUserQuestion` multiple-choice prompt - it's a secret only the user has,
so just ask in plain chat and wait for their reply. Tell them how to get
one if they don't already have it:

> Run `claude setup-token` on a machine where you're logged into the
> Claude Pro/Max subscription you want this stack to use. It prints a
> token starting with `sk-ant-oat01-`.

Remind them briefly why this matters before they paste it: this token
gets shared by everyone who calls the gateway (see README.md's "Cost
control" section), so make sure it's the subscription they actually want
to dedicate to this. Once they paste it, write it into `.env`'s
`CLAUDE_CODE_OAUTH_TOKEN` line.

## 4. Generate the remaining secrets

For whichever of `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`,
`CLAUDEBOX_API_MODE_TOKEN`, and `POSTGRES_PASSWORD` still have placeholders,
ask the user (this one *is* a good fit for `AskUserQuestion`, since it's a
clean either/or) whether they want these auto-generated or want to set
them manually.

**Why this choice exists**: these are just random secrets with no meaning
of their own (unlike the OAuth token), so there's nothing wrong with
generating them - but some users manage secrets through their own
tooling (a password manager, a company secrets vault) and would rather
paste in values they generated elsewhere.

- **Auto-generate**: run `.claude/skills/setup-claudehost/scripts/generate_secrets.sh`
  and merge its `KEY=VALUE` output lines into `.env`, replacing only the
  lines that still say `CHANGE-ME`.
- **Manual**: show the user the commands from that same script (openssl
  one-liners), then wait for them to say they've filled in `.env` before
  continuing. Re-run the `grep CHANGE-ME .env` check from step 2 to
  confirm nothing was missed before moving on.

## 5. Git access for agentic sessions (optional)

Ask the user whether agentic sessions will need to `git clone`/`push` to
private repos. If not, skip this entirely - it's not required for the
stack to work, only for Claude Code to authenticate to git from inside a
workspace.

If yes and `./ssh/claudebox` doesn't already exist:

```bash
mkdir -p ssh
ssh-keygen -t ed25519 -f ssh/claudebox -N ""
cat ssh/claudebox.pub
```

Tell the user to add the printed public key as a deploy key (or a bot
account's key) on their git host.

## 6. Boot the stack

```bash
docker compose up -d --build
```

Then run the bundled wait script rather than guessing at a sleep duration
- claudebox's first boot installs Claude Code from npm, which has been
observed to take anywhere from ~5 seconds to ~2 minutes depending on
registry latency, so a fixed short sleep will falsely report failure:

```bash
.claude/skills/setup-claudehost/scripts/wait_for_stack.sh
```

If it times out, check `docker compose logs claudebox` and
`docker compose logs litellm` before assuming something is broken - it
may just need more time on a slow connection (rerun with
`TIMEOUT=300 .claude/skills/setup-claudehost/scripts/wait_for_stack.sh`).

## 7. Prove it actually works

"Containers are up" doesn't mean the OAuth token is valid or that
claudebox can actually reach Anthropic - the only real confirmation is a
successful response. Source the master key from `.env` and run the smoke
test script, which mints a virtual key scoped to all four models and
sends one real chat completion through it:

```bash
LITELLM_MASTER_KEY=$(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2)
.claude/skills/setup-claudehost/scripts/smoke_test.sh "$LITELLM_MASTER_KEY"
```

If this fails, don't report success - debug from `docker compose logs
claudebox` (auth errors show up there) before telling the user it's ready.

## 8. Report back

Tell the user, concretely:

- The LiteLLM admin UI: `http://localhost:4000/ui`, username `admin`,
  password = their `LITELLM_MASTER_KEY`.
- The virtual key the smoke test minted, and that it's scoped to
  `claude-haiku`/`claude-sonnet`/`claude-opus`/`claude-opusplan` - real
  developer keys should probably be scoped more narrowly per person/team
  (see README.md's "Setup" step 5).
- If more than one person will use this: point them at README.md's
  "Per-developer instances" section rather than having everyone share the
  one subscription/token just configured.
