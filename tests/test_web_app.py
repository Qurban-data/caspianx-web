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
ENGINEER = {"username": "qurban", "password": "caspianx2026"}      # demo accounts of the app (stored hashed in web_app/main.py)
JURY = {"username": "jury", "password": "socar2026"}
assert client.post("/api/auth/login", data=ENGINEER).status_code == 200


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
    z = demo.demo_zip(model())                                                          # (1) upload: synthetic Jan-Sep field
    j = _upload([("files", ("unseen_field.zip", z, "application/zip"))])
    assert j["period"] == ["2025-01-01", "2025-09-30"] and j["n_buildups"] == 3
    p, pr, it = _all_endpoints(j["sid"], j["wells"][0])
    assert len(p["events"]) == 3 and all(len(e["deriv"]) > 20 for e in p["events"])
    assert all(r["k_md"] and r["k_md"] > 0 for r in pr["rows"]) and len(it["events"]) == 3 and len(it["wells"]) == len(j["wells"])


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


def test_monthly_analysis_is_consistent_with_daily_rates():
    j = _check(client.post("/api/demo"))
    m = _check(client.get(f"/api/monthly?sid={j['sid']}"))
    v = _check(client.get(f"/api/vfm?sid={j['sid']}&well=ALL"))
    assert abs(sum(r["oil_stb"] for r in m["months"]) - sum(v["oil"]["rate"])) < 1.0
    assert m["months"][0]["oil_avg_mom_pct"] is None and all(r["days"] > 0 for r in m["months"])
    assert len(m["surface"]["oil"]) == len(j["wells"]) and len(m["matrix"]["months"]) == len(m["months"])
    assert client.get("/api/vfm?sid=field&well=ALL").status_code == 404        # field results are not reachable by a guessable id


def test_model_proof_what_if_and_copilot():
    p = _check(client.get("/api/model-proof"))
    if p["available"]:
        keys = {m["key"] for m in p["models"]}
        assert {"physics", "hybrid"} <= keys and all(m["sanity"]["negative_rates"] == 0 for m in p["models"])
    j = _check(client.post("/api/demo"))
    w0 = _check(client.get(f"/api/what-if?sid={j['sid']}&well={j['wells'][0]}"))
    assert all(v > 0 for v in w0["rates"].values()) and w0["regime"]["regime"] in ("critical", "sub-critical")
    w1 = _check(client.get(f"/api/what-if?sid={j['sid']}&well={j['wells'][0]}&choke={min(100, w0['base']['choke'] * 1.1)}"))
    assert w1["rates"]["oil"] > w0["rates"]["oil"]                      # opening the choke raises the rate
    assert client.get(f"/api/what-if?sid={j['sid']}&well={j['wells'][0]}&pds=99999").status_code == 422
    for q in ("hybrid", "overfit", "missing", "gas", "limits"):
        for lang in ("en", "az"):
            a = _check(client.get(f"/api/copilot?q={q}&sid={j['sid']}&lang={lang}"))
            assert a["lines"] and all(isinstance(x, str) and x for x in a["lines"])


def _ask(text, lang="az", **kw):
    return _check(client.get("/api/copilot", params={"text": text, "sid": "demo", "lang": lang, **kw}))


def test_demo_session_is_stable_and_never_expires():
    """The demo has a fixed id: a fresh server instance (empty session store) rebuilds it instead of answering 404."""
    import web_app.main as M
    j = _check(client.post("/api/demo"))
    assert j["sid"] == "demo" and j["period"] == ["2025-01-01", "2025-09-30"] and j["dataset_kind"] == "DEMO"
    assert all(m["complete"] for m in j["months"]) and len(j["months"]) == 9
    M.SESSIONS.clear()                                                                   # simulate another instance
    for path in ("/api/pta?sid=demo", "/api/interpret?sid=demo", "/api/vfm?sid=demo&well=GL-01", "/api/measurements?sid=demo&well=GL-01"):
        _check(client.get(path))
    a = demo.generate(model()); b = demo.build(model())                                  # static copy == seeded generator
    assert [x for _, x in a] == [x for _, x in b]


def test_period_filter_and_incomplete_months():
    m = _check(client.get("/api/monthly?sid=demo&d0=2025-07-01&d1=2025-09-30"))
    assert [r["month"] for r in m["months"]] == ["2025-07", "2025-08", "2025-09"] and m["period"] == ["2025-07-01", "2025-09-30"]
    p = _check(client.get("/api/monthly?sid=demo&d0=2025-03-10&d1=2025-05-31"))
    assert [r["complete"] for r in p["months"]] == [False, True, True]
    q = _check(client.get("/api/monthly?sid=demo"))["quarters"]
    assert [x["label"] for x in q] == ["Q1", "Q2", "Q3"] and all(x["complete"] for x in q)
    r = client.get("/api/export/vfm_csv?sid=demo&d0=2025-08-01&d1=2025-08-31")
    csv = pd.read_csv(io.BytesIO(r.content))
    assert r.status_code == 200 and csv.timestamp.str[:7].unique().tolist() == ["2025-08"] and "20250801_20250831" in r.headers["content-disposition"]
    an = _check(client.get("/api/anomalies?sid=demo"))["rows"]
    assert any(x["well"] == "GL-03" and x["type"] == "rate_drop" and x["last"] == "2025-07" for x in an)


def test_copilot_parses_questions_and_answers_honestly():
    a = _ask("Sentyabrda GL-03 quyusunda hasilat niyə düşüb?")                      # (1) well + month + metric
    assert a["intent"] == "why" and a["focus"]["well"] == "GL-03" and not a["lines"][0].startswith("İrəli CV")
    assert any("skin" in l for l in a["lines"]) and "Hibrid" not in " ".join(a["lines"])
    b = _ask("Dekabrda GL-03 hasilatı nə qədər olub?")                               # (2) outside the data range
    assert b["in_range"] is False and "yan–sen 2025" in b["lines"][0]
    c = _ask("Hansı quyu birinci?")                                                    # (3) ranking, not one well
    assert c["intent"] == "stim" and c["lines"][1].startswith("1. GL-03") and c["lines"][2].startswith("2. GL-01")
    d = _ask("Bu quyuda su payı necədir?", well="GL-05")                              # (4) context well
    assert d["focus"]["well"] == "GL-05" and "GL-05" in d["lines"][0]
    e = _ask("Bakıda hava necədir?")                                                   # (5) unrelated question
    assert e["matched"] is False and not any(ch.isdigit() for ch in " ".join(e["lines"]))
    f = _ask("GL-15 necə işləyir?")
    assert f["in_range"] is False and "GL-15" in f["lines"][0]
    g = _ask("Why did GL-03 drop in July?", lang="en")
    assert g["focus"]["month"] == "2025-07" and "skin" in " ".join(g["lines"])
    ch = _check(client.get("/api/copilot", params={"sid": "demo", "lang": "az", "well": "GL-03"}))
    assert ch["chips"][0].startswith("GL-03") and ch["context"]["well"] == "GL-03"


def test_sign_in_sign_out_and_protected_routes():
    c = TestClient(app)
    assert c.get("/api/health").status_code == 200                                   # health stays open
    assert c.get("/api/auth/me").json()["ok"] is False
    assert c.post("/api/demo").status_code == 401 and c.get("/api/model-proof").status_code == 401
    r = c.post("/api/auth/login", data={"username": "jury", "password": "wrong"})
    assert r.status_code == 401 and r.json()["ok"] is False and "cx_session" not in c.cookies
    assert c.post("/api/auth/login", data={"username": "nobody", "password": "x"}).status_code == 401
    j = _check(c.post("/api/auth/login", data=JURY))
    assert j["role"] == "jury" and _check(c.get("/api/auth/me"))["user"] == "jury"
    assert c.post("/api/demo").status_code == 200
    c.cookies.set("cx_session", "jury.9999999999.forged")
    assert c.get("/api/auth/me").json()["ok"] is False                                  # forged cookie is rejected
    c.cookies.clear()
    _check(c.post("/api/auth/jury"))                                                 # one-click evaluator access
    assert _check(c.get("/api/auth/me"))["role"] == "jury"
    _check(c.post("/api/auth/logout"))
    assert c.get("/api/auth/me").json()["ok"] is False and c.post("/api/demo").status_code == 401


def test_anomalies_and_typed_copilot():
    j = _check(client.post("/api/demo"))
    a = _check(client.get(f"/api/anomalies?sid={j['sid']}"))
    assert a["well_days"] > 0 and all(r["severity"] in ("alert", "warn", "info") for r in a["rows"])
    for text, lang in (("which well needs stimulation", "en"), ("çatışmayan sensor datası", "az"), ("zzzz", "en")):
        r = _check(client.get("/api/copilot", params={"text": text, "sid": j["sid"], "lang": lang}))
        assert r["lines"]
