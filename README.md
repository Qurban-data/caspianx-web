# CaspianX · Well Intelligence (web app)

FastAPI web app for SOCAR Hackathon 2026: virtual flow metering (validated Physics + Ridge blend, `web_app/models/vfm_model_FINAL.pkl`)
with P10–P90 bands, automated PTA (Horner / Bourdet) and engineering alerts.

No organiser data is stored in this repository. Load data through the app (CSV, Excel, JSON or ZIP) or use **Load demo data** (synthetic).

## Run locally
```
pip install -r web_app/requirements.txt
uvicorn web_app.main:app --port 8000
```
Windows: `tools/START_WEB_APP.bat`.

## Deploy (Render)
New → Blueprint → select this repository. `render.yaml` builds the `Dockerfile` and checks `/api/health`.
