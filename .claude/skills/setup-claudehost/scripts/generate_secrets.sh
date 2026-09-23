#!/bin/sh
# Prints KEY=VALUE lines for the secrets claudehost needs, using the same
# generation patterns documented in .env.example / README.md. Doesn't touch
# .env itself - the caller decides how to merge these in.
set -eu

printf 'LITELLM_MASTER_KEY=sk-%s\n' "$(openssl rand -base64 32 | tr -d '=+/')"
printf 'LITELLM_SALT_KEY=sk-%s\n' "$(openssl rand -base64 32 | tr -d '=+/')"
printf 'CLAUDEBOX_API_MODE_TOKEN=%s\n' "$(openssl rand -hex 32)"
printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -base64 24 | tr -d '=+/')"

# Jira coding agent (orchestrator/) webhook secrets - not needed unless you use it.
printf 'GH_WEBHOOK_SECRET=%s\n' "$(openssl rand -hex 32)"
printf 'JIRA_WEBHOOK_SECRET=%s\n' "$(openssl rand -hex 32)"
