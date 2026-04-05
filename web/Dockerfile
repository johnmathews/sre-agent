# syntax=docker/dockerfile:1.7

# ---------- Stage 1: build the Vue SPA ----------
FROM node:22-alpine AS builder

WORKDIR /app

# Install deps first (leverages layer cache when only source changes)
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ---------- Stage 2: serve dist/ with nginx ----------
FROM nginx:1.27-alpine AS runtime

# nginx:1.19+ runs envsubst on any *.template file in /etc/nginx/templates/
# at container start. Only the listed vars are substituted — anything else
# (e.g. nginx's own $uri) is left alone.
ENV API_UPSTREAM=http://sre-api:8000 \
    NGINX_ENVSUBST_OUTPUT_DIR=/etc/nginx/conf.d \
    NGINX_ENVSUBST_TEMPLATE_SUFFIX=.template \
    NGINX_ENVSUBST_FILTER=API_UPSTREAM

# Remove the default nginx site so our template takes over
RUN rm /etc/nginx/conf.d/default.conf

COPY docker/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD wget -qO- http://localhost/ >/dev/null || exit 1
