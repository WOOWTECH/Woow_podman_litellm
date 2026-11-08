#!/bin/sh
set -eu
PYTHONPATH= python /opt/woow/db_prepare.py
# One container per Quadlet service: Prisma runs before the HTTP socket opens.
PYTHONPATH= DISABLE_SCHEMA_UPDATE=false python /app/litellm/proxy/prisma_migration.py
python /opt/woow/plugin/woow_revoke_ui_sessions.py
if [ "${WOOW_CHATGPT_ENABLED:-}" = true ]; then
  python /opt/woow/plugin/woow_chatgpt.py restore >&2 || \
    echo 'ChatGPT account restore incomplete; the plugin will retry from the database' >&2
fi
exec litellm "$@"
