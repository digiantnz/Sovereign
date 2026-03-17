#!/bin/sh
# Wrapper: maps Sovereign BUSINESS_IMAP_* env vars → community imap-smtp-email format
# Called as: /bin/sh mail_business.sh <script.js> <command> [args...]

export IMAP_HOST="${BUSINESS_IMAP_HOST}"
export IMAP_PORT="${BUSINESS_IMAP_PORT:-993}"
export IMAP_USER="${BUSINESS_IMAP_USER}"
export IMAP_PASS="${BUSINESS_IMAP_PASS}"
export IMAP_TLS="true"
export IMAP_REJECT_UNAUTHORIZED="false"

export SMTP_HOST="${BUSINESS_SMTP_HOST}"
export SMTP_PORT="${BUSINESS_SMTP_PORT:-587}"
export SMTP_USER="${BUSINESS_SMTP_USER}"
export SMTP_PASS="${BUSINESS_SMTP_PASS}"
export SMTP_SECURE="false"
export SMTP_REJECT_UNAUTHORIZED="false"

exec node "/scripts/imap-email/$@"
