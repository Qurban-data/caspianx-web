"""
CaspianX engineer assistant: Claude answers questions grounded ONLY in CaspianX results via tool calls.

* The model never computes rates, skin or permeability: it calls the tools in core.ai_facts (which read the
  validated VFM / PTA / anomaly outputs) and explains them in the engineer's language.
* No API key in code or chat. The key is read from the ANTHROPIC_API_KEY environment variable or from
  Streamlit secrets (.streamlit/secrets.toml: ANTHROPIC_API_KEY = "sk-ant-...").
* Offline / no key / API error -> deterministic grounded answer from core.ai_facts.offline_answer.
"""
from __future__ import annotations

import json
import os

from .ai_facts import TOOL_SPECS, Knowledge, offline_answer

MODEL = os.environ.get("CASPIANX_LLM_MODEL", "claude-opus-5-5")
MAX_TOOL_ROUNDS = 6

SYSTEM = """You are the CaspianX production engineering assistant for {wells_text}, \
used by SOCAR production and reservoir engineers.

Rules:
1. Every number you state must come from a tool result in this conversation. Never estimate, extrapolate or compute \
new rates, skin, permeability or gains yourself. If the tools do not contain the answer, say so plainly.
2. For "why did X drop / rise" questions call explain_change first; for well questions call well_summary; for field \
questions call field_overview. Call more tools when the question needs them (anomalies, pta_results, interventions).
3. Explain with production engineering logic: choke physics (q ~ choke^2 sqrt(WHP - P_ds)), water cut, uptime, \
lift gas, skin. Name the driver with the biggest STB/d share first.
4. Say where each fact comes from: VFM (predicted), PTA (measured pressure), sensors (measured), anomaly detector.
5. Tie the answer to business value when the tools give it (STB/d gained or lost, intervention upside).
6. Answer in the language of the question (Azerbaijani by default). Be short: at most 8 sentences or bullets, then \
one line with the recommended next action."""


def system_prompt(K: "Knowledge | None" = None) -> str:
    """System prompt with the wells of the LOADED dataset (never a hard-coded well list)."""
    wells = []
    try:
        P = getattr(K, "P", None)
        wells = sorted(P["well_name"].unique()) if P is not None else []
    except Exception:
        wells = []
    txt = (f"{len(wells)} gas-lifted wells ({', '.join(wells[:12])}{', ...' if len(wells) > 12 else ''})" if wells
           else "the gas-lifted wells in the loaded dataset")
    return SYSTEM.replace("{wells_text}", txt)


def get_api_key() -> str | None:
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k:
        return k
    try:
        import streamlit as st
        return st.secrets.get("ANTHROPIC_API_KEY")  # type: ignore[attr-defined]
    except Exception:
        return None


def online_allowed() -> bool:
    """Online mode is OPT-IN: competition rule = no data leaves the machine. It is enabled only when the
    operator sets CASPIANX_AI_ONLINE=1 (env) or CASPIANX_AI_ONLINE = "1" in Streamlit secrets."""
    v = os.environ.get("CASPIANX_AI_ONLINE")
    if v is None:
        try:
            import streamlit as st
            v = st.secrets.get("CASPIANX_AI_ONLINE")  # type: ignore[attr-defined]
        except Exception:
            v = None
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _client(api_key: str | None):
    import anthropic
    return anthropic.Anthropic(api_key=api_key, max_retries=1, timeout=60.0)


def ask(K: Knowledge, question: str, history: list[dict] | None = None, client=None, api_key: str | None = None,
        force_offline: bool = False) -> dict:
    """Answer one question. Returns {answer, tools_used, mode: online|offline, error?}.

    ``history`` holds earlier turns as plain {"role", "content": str} pairs (text only, so the request stays small).
    ``client`` can be injected (tests use a fake client).
    """
    if force_offline:
        return offline_answer(K, question)
    if client is None:
        if not online_allowed():
            r = offline_answer(K, question)
            r["note"] = "Oflayn rejim (default): data kompüterdən çıxmır. Onlayn üçün CASPIANX_AI_ONLINE=1."
            return r
        key = api_key or get_api_key()
        if not key:
            r = offline_answer(K, question)
            r["note"] = "API açarı yoxdur: oflayn rejim"
            return r
        try:
            client = _client(key)
        except Exception as ex:  # SDK missing
            r = offline_answer(K, question)
            r["error"] = f"{type(ex).__name__}: {ex}"
            return r

    import anthropic

    messages: list[dict] = [{"role": m["role"], "content": m["content"]} for m in (history or [])[-6:]]
    messages.append({"role": "user", "content": question})
    used: list[str] = []
    try:
        for _ in range(MAX_TOOL_ROUNDS + 1):
            resp = client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=system_prompt(K),
                tools=TOOL_SPECS,
                messages=messages,
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if resp.stop_reason == "refusal":
                r = offline_answer(K, question)
                r["error"] = "model refused; offline answer shown"
                return r
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            if resp.stop_reason != "tool_use":
                text = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
                return {"answer": text or offline_answer(K, question)["answer"], "tools_used": used, "mode": "online",
                        "model": getattr(resp, "model", MODEL)}
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for b in resp.content:
                if getattr(b, "type", "") != "tool_use":
                    continue
                args = b.input if isinstance(b.input, dict) else {}
                out = K.call(b.name, args)
                used.append(b.name)
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out, ensure_ascii=False, default=str)[:60000],
                                **({"is_error": True} if "error" in out else {})})
            messages.append({"role": "user", "content": results})
        r = offline_answer(K, question)
        r["error"] = "too many tool rounds; offline answer shown"
        return r
    except (anthropic.APIConnectionError, anthropic.APITimeoutError) as ex:
        r = offline_answer(K, question)
        r["error"] = f"internet yoxdur ({type(ex).__name__}): oflayn cavab"
        return r
    except anthropic.AuthenticationError:
        r = offline_answer(K, question)
        r["error"] = "API açarı yanlışdır: oflayn cavab"
        return r
    except anthropic.APIStatusError as ex:
        r = offline_answer(K, question)
        r["error"] = f"API xətası {ex.status_code}: oflayn cavab"
        return r
