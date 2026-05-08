#!/bin/bash
# Daily 6am NZT dev review — cron runs this at 18:00 UTC

cd /home/sovereign/sovereign

TTY=$(who | awk -v user="matt" '$1 == user {print "/dev/" $2; exit}')

if [ -z "$TTY" ]; then
    exit 0
fi

{
    echo ""
    echo "=== Morning Dev Review $(date '+%Y-%m-%d %H:%M %Z') ==="
    echo ""
    claude --print "Please review the claude.md and associated .md files and advise what is currently outstanding to develop" | fold -s -w 80
    echo ""
    echo "=== End of Review ==="
    echo ""
} > "$TTY" 2>&1
