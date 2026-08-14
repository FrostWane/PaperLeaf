FROM registry-1.docker.io/library/python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN groupadd --system --gid 1001 paperleaf \
    && useradd --system --uid 1001 --gid paperleaf paperleaf

# 先安装稳定依赖层；普通源码改动不会重新下载 Python 依赖。
COPY pyproject.toml README.md ./
COPY paperleaf_api/__init__.py ./paperleaf_api/__init__.py
RUN pip install --no-cache-dir .

# Git SHA 只影响运行时代码层，不能击穿稳定的 Python 依赖缓存。
ARG PAPERLEAF_GIT_SHA=unknown
ENV PAPERLEAF_GIT_SHA=${PAPERLEAF_GIT_SHA}

# 再复制运行时代码与迁移。API、Worker、Migrate 共用此 Dockerfile/context，可复用上述层。
COPY paperleaf_api ./paperleaf_api
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
RUN mkdir -p /app/data/uploads \
    && chown -R paperleaf:paperleaf /app

USER paperleaf
EXPOSE 8000
CMD ["uvicorn", "paperleaf_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
