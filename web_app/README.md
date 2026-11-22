# CaspianX Well Intelligence: web app (FastAPI, no Streamlit)

The app is a single page built with HTML, CSS and Plotly.js; plotly.js is vendored, so no CDN or web fonts are needed.
It runs on a FastAPI backend that calls the existing `core/` pipeline directly. The VFM model is
`models/vfm_model_FINAL.pkl`: the submitted REF v2 blend, refitted on all 85 tests.

## Run locally (from the repository root)
```
pip install -r web_app/requirements.txt
uvicorn web_app.main:app --host 0.0.0.0 --port 8000
```
Then open http://localhost:8000. To share it temporarily: `cloudflared tunnel --url http://localhost:8000`.

## API
| Deliverable | Endpoint |
|---|---|
| 1. Upload (CSV / Excel / ZIP / event JSON, drag and drop) | `POST /api/upload` (multipart `files`) |
| 1. Demo data | `POST /api/demo` (synthetic field, not field data) |
| 2. Field measurements | `GET /api/measurements?sid=&well=` |
| 3. Daily oil / water / gas + P10–P90 | `GET /api/vfm?sid=&well=` (`well=ALL` for the field) |
| 4. PTA plots: log-log + Bourdet, Horner, MDH, Cartesian | `GET /api/pta?sid=` |
| 5. k, skin, m, ΔP_skin | `GET /api/properties?sid=` |
| 6. Interpretation, intervention priority, alerts | `GET /api/interpret?sid=&oil_price=` |
| 7. Exports | `GET /api/export/{vfm_csv,pta_csv,xlsx,report_html}?sid=` |

There are also `GET /api/health` and `GET /api/docs` (OpenAPI).

Smoke test: `python -m pytest tests/test_web_app.py -q`.

## Deploy (Render.com)
`render.yaml` and the `Dockerfile` are at the repository root. The Docker build context excludes `data/`, `outputs/`
and every other data folder (see `.dockerignore`).
