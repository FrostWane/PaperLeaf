ARG NODE_IMAGE=public.ecr.aws/docker/library/node:22-bookworm-slim
FROM ${NODE_IMAGE} AS dependencies
WORKDIR /app
RUN corepack enable && corepack install --global pnpm@9.7.0
COPY package.json pnpm-lock.yaml ./
COPY patches ./patches
RUN pnpm install --frozen-lockfile

FROM dependencies AS build
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api/v1
ARG NEXT_PUBLIC_DATA_MODE=real
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL} \
    NEXT_PUBLIC_DATA_MODE=${NEXT_PUBLIC_DATA_MODE}
COPY . .
RUN pnpm build

FROM ${NODE_IMAGE} AS production-dependencies
WORKDIR /app
RUN corepack enable && corepack install --global pnpm@9.7.0
COPY package.json pnpm-lock.yaml ./
COPY patches ./patches
RUN pnpm install --prod --frozen-lockfile

FROM ${NODE_IMAGE} AS runtime
ENV NODE_ENV=production \
    HOSTNAME=0.0.0.0 \
    PORT=3000 \
    NEXT_PUBLIC_DATA_MODE=real
WORKDIR /app
RUN groupadd --system --gid 1001 paperleaf \
    && useradd --system --uid 1001 --gid paperleaf paperleaf
COPY --from=production-dependencies --chown=paperleaf:paperleaf /app/node_modules ./node_modules
COPY --from=build --chown=paperleaf:paperleaf /app/dist ./dist
COPY --from=build --chown=paperleaf:paperleaf /app/package.json ./package.json
USER paperleaf
EXPOSE 3000
CMD ["node", "node_modules/vinext/dist/cli.js", "start", "--host", "0.0.0.0", "--port", "3000"]
