FROM python:3.11-slim

WORKDIR /app

RUN groupadd --system --gid 1002 paperleaf-mcp \
    && useradd --system --uid 1002 --gid paperleaf-mcp paperleaf-mcp

COPY pyproject.toml ./
COPY academic_search_mcp/__init__.py ./academic_search_mcp/__init__.py
RUN pip install --no-cache-dir .

COPY academic_search_mcp ./academic_search_mcp
RUN chown -R paperleaf-mcp:paperleaf-mcp /app

USER paperleaf-mcp
EXPOSE 8080

CMD ["uvicorn", "academic_search_mcp.server:app", "--host", "0.0.0.0", "--port", "8080"]
