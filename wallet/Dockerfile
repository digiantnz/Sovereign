FROM node:18-alpine

RUN apk add --no-cache curl

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

COPY wallet-api.js \
     wallet-rpc.js \
     wallet-watchlist.js \
     wallet-watcher.js \
     wallet-harness-a2a.js \
     wallet-harness-portfolio.js \
     ./

EXPOSE 3001

CMD ["node", "wallet-api.js"]
