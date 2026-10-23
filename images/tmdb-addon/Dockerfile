FROM node:24.22.1-alpine@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53 AS builder
ARG TMDB_ADDON_REF=a32301c074f787ff45bf0541d6e763d080aba9bb
WORKDIR /app
RUN apk add --no-cache git \
 && git init \
 && git remote add origin https://github.com/mrcanelas/tmdb-addon.git \
 && git fetch --depth 1 origin "$TMDB_ADDON_REF" \
 && git checkout FETCH_HEAD \
 && mkdir -p /image/meta \
 && cp package.json /image/meta/ \
 && if [ -f package-lock.json ]; then cp package-lock.json /image/meta/; fi \
 && if [ -f package-lock.json ]; then npm ci; else npm install; fi \
 && npm run build
FROM node:24.22.1-alpine@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53
WORKDIR /app
COPY --from=builder /image/meta/ ./
RUN if [ -f package-lock.json ]; then npm ci --omit=dev; else npm install --omit=dev; fi
COPY --from=builder /app/addon ./addon
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/public ./public
USER node
EXPOSE 1337
ENTRYPOINT ["node", "addon/server.js"]
