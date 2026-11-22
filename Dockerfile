# CaspianX Well Intelligence - FastAPI web app (no Streamlit). Build context = repository root.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=7860
WORKDIR /app
COPY web_app/requirements.txt web_app/requirements.txt
RUN pip install --no-cache-dir -r web_app/requirements.txt
COPY core/ core/
COPY templates/ templates/
COPY web_app/ web_app/
RUN useradd -m app && chown -R app /app
USER app
EXPOSE 7860
HEALTHCHECK CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"7860\")}/api/health')" || exit 1
CMD ["sh", "-c", "uvicorn web_app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
