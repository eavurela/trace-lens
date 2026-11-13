FROM python:3.12-slim
RUN useradd --uid 1000 --create-home app && mkdir -p /data && chown app:app /data
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
USER app
ENV TRACE_LENS_DB=/data/trace-lens.db TRACE_LENS_PORT=7500 PYTHONUNBUFFERED=1
EXPOSE 7500
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7500/health', timeout=4)" || exit 1
CMD ["trace-lens"]
