FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install

# util-linux provides nsenter — required for pid:host commands (systemctl, journalctl, apt_check)
# python3 + pip + pylint + semgrep — required for Dev-Harness Phase 1 local analysis
# jq — required by dev_analyse.sh for NDJSON tool-envelope output
# git — required by dev_git_diff for uncommitted change diffs
RUN apk add --no-cache util-linux python3 py3-pip jq git && \
    pip3 install --no-cache-dir --break-system-packages pylint semgrep

COPY . .
CMD ["node", "index.js"]
