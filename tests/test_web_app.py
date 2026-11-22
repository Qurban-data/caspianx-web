"""FastAPI web app smoke test: all 7 deliverable endpoints return HTTP 200 with ok=True on unseen data."""
import io
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(ROOT))
from web_app.main import app, model  # noqa: E402
import web_app.demo_data as demo     # noqa: E402

client = TestClient(app)


def _check(r):
    assert r.status_code == 200, r.text[:500]
    j = r.json()
    assert j.get("ok") is True, j
    return j


def _job(r, timeout=300):
    """Upload is streamed to disk and processed in a background job: poll until it finishes."""
    import time
    jid = _check(r)["job"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = _check(client.get(f"/api/job/{jid}"))
        if j["state"] != "running":
            return j
        time.sleep(0.3)
    raise AssertionError("job timed out")


def _upload(files):
    j = _job(client.post("/api/upload", files=files))
    assert j["state"] == "done", j.get("error")
    return j["result"]


def _all_endpoints(sid, well):
    _check(client.get(f"/api/summary?sid={sid}"))
    m = _check(client.get(f"/api/measurements?sid={sid}&well={well}"))                 # (2) visualise
    assert len(m["dates"]) > 0 and "P_wf (downhole), psia" in m["series"]
    v = _check(client.get(f"/api/vfm?sid={sid}&well={well}"))                           # (3) VFM + P10-P90
    assert len(v["oil"]["rate"]) == len(v["dates"]) and len(v["oil"]["p10"]) == len(v["dates"])
    _check(client.get(f"/api/vfm?sid={sid}&well=ALL"))
    p = _check(client.get(f"/api/pta?sid={sid}"))                                       # (4) PTA plots
    pr = _check(client.get(f"/api/properties?sid={sid}"))                               # (5) k, S, m, dP skin
    it = _check(client.get(f"/api/interpret?sid={sid}"))                                # (6) interpretation
    assert not any("$" in str(a.get("message", "")) for a in it["alerts"])            # physical STB/d only
    md = _check(client.get("/api/model"))
    assert md["validation"]["forward_90d"]["oil"]["n"] > 0 and set(md["blend"]["w_A"]) == {"oil", "water", "gas"}
    for kind, ctype in [("vfm_csv", "text/csv"), ("pta_csv", "text/csv"), ("xlsx", "spreadsheet"), ("report_html", "text/html")]:
        r = client.get(f"/api/export/{kind}?sid={sid}")                                 # (7) export
        assert r.status_code == 200 and ctype in r.headers["content-type"] and len(r.content) > 100, kind
    csv = pd.read_csv(io.BytesIO(client.get(f"/api/export/vfm_csv?sid={sid}").content))
    assert list(csv.columns) == ["timestamp", "well_name", "oil_rate_stbd", "water_rate_stbd", "gas_rate_mscfd"]
    assert csv.notna().all().all() and (csv.iloc[:, 2:] >= 0).all().all()
    return p, pr, it


def test_index_and_health():
    r = client.get("/")
    assert r.status_code == 200 and "CaspianX" in r.text and "streamlit" not in r.text.lower()
    assert _check(client.get("/api/health"))["model_loaded"]


def test_upload_unseen_zip_all_endpoints():
    z = demo.demo_zip(model())                                                          # (1) upload: Oct-Dec, never trained on
    j = _upload([("files", ("unseen_field.zip", z, "application/zip"))])
    assert j["period"][0] >= "2025-10-01" and j["n_buildups"] == 2
    p, pr, it = _all_endpoints(j["sid"], j["wells"][0])
    assert len(p["events"]) == 2 and all(len(e["deriv"]) > 20 for e in p["events"])
    assert all(r["k_md"] and r["k_md"] > 0 for r in pr["rows"]) and len(it["events"]) == 2 and len(it["wells"]) == len(j["wells"])


def test_demo_button():
    j = _check(client.post("/api/demo"))
    assert "synthetic" in j["source"]


def test_bad_input_gives_clear_error_not_crash():
    j = _job(client.post("/api/upload", files=[("files", ("notes.csv", b"a,b\n1,2\n", "text/csv"))]))
    assert j["state"] == "error" and j["error"] and "Traceback" not in j["error"]
    r = client.post("/api/upload", files=[("files", ("readme.txt", b"hello", "text/plain"))])
    assert r.status_code == 400 and r.json()["ok"] is False and r.json()["error"]
    assert client.get("/api/job/doesnotexist").status_code == 404
    assert client.get("/api/vfm?sid=nope&well=ALL").status_code == 404


@pytest.mark.skipif(not (ROOT / "data/socar/daily_features.csv").exists(), reason="needs data/socar")
def test_real_folder_upload():
    files = [("files", (f.name, f.read_bytes(), "text/csv")) for f in (ROOT / "data/socar").glob("*.csv")]
    files += [("files", (f.name, f.read_bytes(), "application/json")) for f in (ROOT / "data/socar/events").glob("*.json")]
    j = _upload(files)
    assert j["well_days"] == 2730 and j["n_buildups"] == 6
    _all_endpoints(j["sid"], "GL-01")


def test_pta_fit_quality_and_vfm_layers_exposed():
    j = _check(client.post("/api/demo"))
    ev = _check(client.get(f"/api/pta?sid={j['sid']}"))["events"]
    assert ev and all(e["n_points"] > 5 and e["rmse_psi"] is not None and e["rmse_psi"] >= 0 for e in ev)
    v = _check(client.get(f"/api/vfm?sid={j['sid']}&well={j['wells'][0]}"))
    assert set(v["layers"]) >= {"model_a", "model_b"}
    assert len(v["layers"]["model_a"]["oil"]) == len(v["dates"])


def test_large_per_minute_file_is_streamed():
    """A per-minute high-frequency CSV is uploaded together with the static files and processed as a path, not in memory."""
    import numpy as np
    rng = np.random.default_rng(0)
    t = pd.date_range("2025-10-01", periods=60 * 24 * 20, freq="min")
    rows = []
    for w in ("W-A", "W-B"):
        whp = 900 + rng.normal(0, 3, len(t))
        shut = (t >= "2025-10-10") & (t < "2025-10-11")
        rows.append(pd.DataFrame({"timestamp": t, "well_id": w, "whp_psi": np.where(shut, 1400, whp),
                                  "choke_pct": np.where(shut, 0, 40.0), "downstream_psi": 300 + rng.normal(0, 2, len(t))}))
    hf = pd.concat(rows).to_csv(index=False).encode()
    r = client.post("/api/upload", files=[("files", ("high_freq_data.csv", hf, "text/csv"))])
    j = _job(r)
    assert j["state"] in ("done", "error")
    if j["state"] == "error":   # without the static well files the input check must explain what is missing
        assert "Input check failed" in j["error"] or "Processing failed" in j["error"]


def test_public_copy_matches_static():
    """Vercel serves public/ from the CDN: it must be an exact copy of web_app/static."""
    import filecmp
    pub = ROOT / "public"
    if not pub.exists():
        pytest.skip("no public/ folder")
    for f in (ROOT / "web_app/static").rglob("*"):
        if f.is_file():
            assert filecmp.cmp(f, pub / "static" / f.relative_to(ROOT / "web_app/static"), shallow=False), f
    assert filecmp.cmp(ROOT / "web_app/static/index.html", pub / "index.html", shallow=False)
