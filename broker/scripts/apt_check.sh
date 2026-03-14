#!/bin/sh
# apt_check.sh — Check host OS for available package updates via nsenter into host PID 1.
# Runs inside the broker container (Alpine) but executes apt in the host's mount namespace.
# Requires: pid:host in compose.yml + nsenter (util-linux) in broker Dockerfile.
# Output: apt list --upgradable results (one package per line).
set -e

# Refresh package index quietly, then list upgradable packages
nsenter -t 1 -m -u -i -n -- sh -c 'apt-get -qq update 2>/dev/null; apt list --upgradable 2>/dev/null'
