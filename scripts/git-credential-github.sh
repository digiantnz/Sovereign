#!/usr/bin/env bash
# Git credential helper for github.com — reads GITHUB_PAT from secrets/github.env
# so the token lives in exactly one place on disk, never in .git/config.
#
# Install: git config credential.helper /home/sovereign/sovereign/scripts/git-credential-github.sh
set -euo pipefail

[ "${1:-}" = "get" ] || exit 0

source /home/sovereign/sovereign/secrets/github.env

echo "username=x-access-token"
echo "password=${GITHUB_PAT}"
