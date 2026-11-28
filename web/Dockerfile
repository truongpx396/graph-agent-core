# The web client's image (the `web` service; RUNTIME_PATTERNS.md 116): Vite builds the page with Node, and Caddy serves it. Node never reaches the shipped
# image. The context is this directory, so nothing of the app can get in. Both images are pinned by digest; Dependabot proposes new ones (.github/dependabot.yml).

# The output is the same for every target, so it is built on the builder's own platform (`$BUILDPLATFORM`): no emulation.
# The multi-architecture index of node:22.23.5-slim, a security release of 2026-11-11 (.nvmrc and CI use Node 22 too).
FROM --platform=$BUILDPLATFORM node:22.23.5-slim@sha256:be979e5523c76fa4d5ca4ebd3099078f220aa47ec39c48ad5b5df9314debd6eb AS build
WORKDIR /web
# The lock first, so a source-only change does not reinstall.
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY . .
# The server fills the sign-in placeholder per request (server/Caddyfile, `templates`). `grep` first: a renamed placeholder must fail the build, not ship a page
# that takes the placeholder for its configuration.
RUN npm run build \
    && grep -q __AGENT_AUTH__ dist/index.html \
    && sed -i 's/__AGENT_AUTH__/{{include "agent-auth.tmpl" | html}}/' dist/index.html

# The same Caddy release the front door runs (deploy/dev/gateway.yml).
FROM caddy:2.11.7@sha256:504a44d7d74bd2bc28a66afc4774912fc21223ca8a67734af1c54ab2f999b489 AS runtime
# `apk upgrade`: security patches for what the base image already holds, as the app image does. The official binary also carries a file capability (to bind
# ports under 1024), and with `cap_drop: ALL` the kernel refuses to start such a file at all; this server listens on 8080 and needs none.
RUN apk upgrade --no-cache \
    && apk add --no-cache --virtual .setcap libcap \
    && setcap -r /usr/bin/caddy \
    && apk del .setcap
COPY server/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /web/dist /srv
COPY server/agent-auth.tmpl /srv/agent-auth.tmpl
# Caddy writes under these even with nothing to store; /tmp is the one writable place when the filesystem is read-only.
ENV XDG_CONFIG_HOME=/tmp/config XDG_DATA_HOME=/tmp/data
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
