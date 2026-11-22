"""Vercel entrypoint: the FastAPI app. Static assets are served from public/ by the CDN."""
from web_app.main import app  # noqa: F401
