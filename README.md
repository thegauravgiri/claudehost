# claudehost

Turn a Claude Code subscription into a self-hosted, OpenAI-compatible API
your whole team can call: normal chat completions, and real agentic coding
sessions (bash/file access against a repo) using the same Claude Code you'd
run from the CLI. No per-token billing, no hand-built wrapper.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker Compose](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)](docker-compose.yml)

## Quick start

```bash
cp .env.example .env
```

Fill in `.env`:
- Four secrets, generated however you like, e.g. `openssl rand -base64 32`
  (see the comments in `.env.example` for which command fits which one)
- `CLAUDE_CODE_OAUTH_TOKEN`: run `claude setup-token` on a machine logged
  into the Claude Pro/Max subscription you want this to use, paste the result

Then:

```bash
docker compose up -d --build
```

Create a key and test it:

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

That's it. If you'd rather not do this by hand, run the `setup-claudehost`
skill instead: it does all of the above conversationally and proves the
whole chain works with a real request.

## How it works

```
developer -> litellm :4000 -> claudebox :8080 (internal only) -> Claude Code
```

Chat normally, or point a request at a real repo checked out under
`./workspaces/<name>` by adding the header `X-Aicodebox-Workspace: <name>` -
same endpoint, but now it's an agent with real bash/file access to that repo,
not just a chatbot.

## Good to know

- **One shared subscription by default.** Everyone's traffic shares one
  Claude account's rate limit and cost. Fine for small teams; for ~10+
  people, see [per-developer instances](docs/ADVANCED.md#per-developer-instances).
- **`claudebox` gets real shell access.** Read its source before trusting it
  with your subscription token - see [security notes](docs/ADVANCED.md#security-notes).
- Cost tradeoffs, workspace routing details, config reference, and
  troubleshooting all live in [docs/ADVANCED.md](docs/ADVANCED.md).

## License

[MIT](LICENSE)
