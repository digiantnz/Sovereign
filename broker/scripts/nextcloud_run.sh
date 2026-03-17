#!/bin/sh
# Wrapper: maps Sovereign WebDAV env vars → community openclaw-nextcloud format
# Called as: /bin/sh nextcloud_run.sh <command> <subcommand> [args...]
# e.g.      /bin/sh nextcloud_run.sh calendar list --from 2026-03-01

# Nextcloud base URL — internal Docker URL on business_net
# WEBDAV_BASE = http://nextcloud/remote.php/dav/files/digiant — strip the WebDAV path
NC_URL="${NEXTCLOUD_URL:-http://nextcloud}"
export NEXTCLOUD_URL="${NC_URL}"
export NEXTCLOUD_USER="${WEBDAV_USER:-digiant}"
export NEXTCLOUD_TOKEN="${WEBDAV_PASS}"

exec node /scripts/nextcloud/nextcloud.js "$@"
