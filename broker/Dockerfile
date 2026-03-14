FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install

# util-linux provides nsenter — required for pid:host commands (systemctl, journalctl, apt_check)
RUN apk add --no-cache util-linux

COPY . .
CMD ["node", "index.js"]
