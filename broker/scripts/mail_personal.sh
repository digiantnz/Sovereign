#!/bin/sh
# Wrapper: maps Sovereign PERSONAL_IMAP_* env vars → community imap-smtp-email format
# Called as: /bin/sh mail_personal.sh <script.js> <command> [args...]
# e.g.      /bin/sh mail_personal.sh imap.js check --limit 10

export IMAP_HOST="${PERSONAL_IMAP_HOST}"
export IMAP_PORT="${PERSONAL_IMAP_PORT:-993}"
export IMAP_USER="${PERSONAL_IMAP_USER}"
export IMAP_PASS="${PERSONAL_IMAP_PASS}"
export IMAP_TLS="true"
export IMAP_REJECT_UNAUTHORIZED="false"

export SMTP_HOST="${PERSONAL_SMTP_HOST}"
export SMTP_PORT="${PERSONAL_SMTP_PORT:-587}"
export SMTP_USER="${PERSONAL_SMTP_USER}"
export SMTP_PASS="${PERSONAL_SMTP_PASS}"
export SMTP_SECURE="false"
export SMTP_REJECT_UNAUTHORIZED="false"

exec node "/scripts/imap-email/$@"
