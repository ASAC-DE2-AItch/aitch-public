"""처분 Brief 생성 — 엔지니어가 고른 웨이퍼별 처분에 **근거 산문**을 붙인다 (2026-08-11).

왜 이 파일이 있나
-----------------
8/10 리허설에서 처분 승인이 정비 카드에 얹혀 있었다: 02 패널은 "진짜 장비 이상" 근거인데
04 는 웨이퍼 폐기를 묻는 기형이었고, 승인 한 번이 정비 발행과 처분 확정을 동시에 했다.
처분을 전용 카드로 분리하면 그 카드의 **02 판정·근거 자리가 빈다** — 이 모듈이 그 자리를
채운다. PM 결정(2026-08-11): brief 는 실제 LLM(vLLM/Qwen3.6)이 쓴다.

경계 (헌법 6-1·6-3)
-------------------
· 사실(facts)은 **DB 에서만** 온다 — 웨이퍼 목록·권고·예측 C65·격리 사유·위반 센서·정지 이력.
  LLM 은 그 사실을 요약·해석만 한다. 수치를 새로 만들지 못하게 프롬프트에 못 박고,
  응답 스키마도 문장 필드만 받는다(숫자 필드 없음 — 표시 수치는 전부 우리가 계산한 값).
· LLM 실패(엔드포인트 부재·타임아웃·파싱 실패)는 **결정적 fallback** 으로 떨어진다:
  conf 0.00 + "LLM 분석 실패 — 수동 검토 요청" (헌법 6-3 패턴, 실값 위장 금지).
  fallback 도 근거 3줄은 실사실로 채운다 — 빈 카드보다 낫고, 전부 DB 인용이라 안전하다.
· httpx 대신 표준 라이브러리(urllib)로 호출한다 — 게이트웨이는 호스트 프로세스라
  agent_service 의 의존성 트리를 공유하지 않는다(설치 상태에 기대지 않는다).

이 모듈은 DB 도 Kafka 도 직접 만지지 않는다. conn 을 받아 읽기만 하고 dict 를 돌려준다.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger("gateway.dispo_brief")

# 응답 스키마 (vLLM guided_json — 토큰 레벨 강제, D13). **문장 필드만** 받는다.
BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"},
                     "minItems": 2, "maxItems": 3},
        "counter_evidence": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["reason", "evidence", "counter_evidence", "confidence"],
    "additionalProperties": False,
}

_SYSTEM = (
    "당신은 반도체 식각 공정의 FDC 처분 심사 보조자다. 엔지니어가 이미 고른 웨이퍼 처분에 "
    "대해, 주어진 사실만으로 근거를 정리한다. 규칙: "
    "① 주어진 사실에 없는 수치·센서·사건을 만들지 마라. "
    "② 처분을 뒤집으라고 하지 마라 — 판단은 엔지니어 몫이다. 다만 반증(놓칠 수 있는 위험)은 "
    "반드시 한 줄로 적는다. "
    "③ 한국어로 쓰고, 각 근거는 한 문장, 수치는 사실에서 그대로 인용한다. "
    "④ confidence 는 근거의 충분성에 대한 당신의 확신(0~1)이다."
)


# ---------------------------------------------------------------------------
# ① 사실 수집 — DB 읽기 전용
# ---------------------------------------------------------------------------
def collect_facts(conn, incident_id: str, wafer_ids: list) -> dict:
    """이 처분 판단에 필요한 사실을 모은다 (읽기 전용·단일 커넥션).

    수집 항목: Incident 헤더 · 대상 웨이퍼 행(권고·격리 사유·근거) · 예측 C65/anomaly ·
    상위 위반 센서 · RTD 정지 이력. 조회 실패는 그 항목만 비우고 계속한다(brief 는 표시물이라
    한 항목 실패가 승인 요청을 막으면 본말전도 — 6-2).
    """
    facts: dict[str, Any] = {"incident_id": incident_id, "wafers": [],
                             "sensors": [], "inhibit": None, "chamber_id": None}
    with conn.cursor() as cur:
        try:
            # ⚠️ incidents 에 alarm_count·first_alert_at 컬럼은 **없다**(init.sql:102) —
            #    알람 수는 incident_alerts 카운트, 시각은 created_at 이 정본이다.
            cur.execute("""SELECT i.chamber_id, i.incident_type, i.severity_max, i.lifecycle,
                                  i.created_at,
                                  (SELECT COUNT(*) FROM incident_alerts ia
                                    WHERE ia.incident_id = i.incident_id) AS alarms
                             FROM incidents i WHERE i.incident_id = %s""", (incident_id,))
            r = cur.fetchone()
            if r:
                facts["chamber_id"] = r[0]
                facts["incident"] = {"type": r[1], "severity": r[2], "lifecycle": r[3],
                                     "created_at": str(r[4]) if r[4] else None,
                                     "alarms": r[5]}
        except Exception:                              # noqa: BLE001
            log.warning("처분 brief — incidents 조회 실패 (계속)", exc_info=True)
        if wafer_ids:
            try:
                cur.execute("""SELECT wafer_id, lot_id, status, system_recommendation,
                                      hold_reason, recommendation_basis
                                 FROM wafer_dispositions
                                WHERE incident_id = %s AND wafer_id = ANY(%s)
                                ORDER BY id""", (incident_id, list(wafer_ids)))
                for w in cur.fetchall():
                    facts["wafers"].append({"wafer_id": w[0], "lot_id": w[1], "status": w[2],
                                            "recommendation": w[3], "hold_reason": w[4],
                                            "basis": w[5]})
            except Exception:                          # noqa: BLE001
                log.warning("처분 brief — wafer_dispositions 조회 실패 (계속)", exc_info=True)
            try:
                cur.execute("""SELECT wafer_id, predicted_c65, actual_c65, anomaly_score
                                 FROM wafer_predictions
                                WHERE wafer_id = ANY(%s)
                                ORDER BY id""", (list(wafer_ids),))
                pred = {p[0]: {"predicted_c65": p[1], "actual_c65": p[2], "anomaly": p[3]}
                        for p in cur.fetchall()}
                for w in facts["wafers"]:
                    w.update(pred.get(w["wafer_id"], {}))
            except Exception:                          # noqa: BLE001
                log.warning("처분 brief — wafer_predictions 조회 실패 (계속)", exc_info=True)
        try:
            cur.execute("""SELECT sensor_id, rule_id, COUNT(*), MAX(severity)
                             FROM spc_violations WHERE incident_id = %s
                            GROUP BY sensor_id, rule_id
                            ORDER BY COUNT(*) DESC LIMIT 5""", (incident_id,))
            facts["sensors"] = [{"sensor": s[0], "rule": s[1], "n": s[2], "severity": s[3]}
                                for s in cur.fetchall()]
        except Exception:                              # noqa: BLE001
            log.warning("처분 brief — spc_violations 조회 실패 (계속)", exc_info=True)
        try:
            cur.execute("""SELECT chamber_id, inhibited_at, released_at
                             FROM chamber_inhibits
                            WHERE incident_id = %s OR release_incident_id = %s
                            ORDER BY id DESC LIMIT 1""", (incident_id, incident_id))
            r = cur.fetchone()
            if r:
                facts["inhibit"] = {"chamber_id": r[0], "inhibited_at": str(r[1]),
                                    "released_at": str(r[2]) if r[2] else None}
        except Exception:                              # noqa: BLE001
            log.warning("처분 brief — chamber_inhibits 조회 실패 (계속)", exc_info=True)
    return facts


# ---------------------------------------------------------------------------
# ② 결정적 요약 — 프롬프트 재료 겸 fallback 근거
# ---------------------------------------------------------------------------
def _pred_range(facts: dict) -> Optional[tuple]:
    vals = [w.get("predicted_c65") for w in facts.get("wafers", [])
            if isinstance(w.get("predicted_c65"), (int, float))]
    return (min(vals), max(vals)) if vals else None


def deterministic_lines(facts: dict, per_wafer: list) -> list:
    """LLM 없이도 성립하는 근거 3줄 — 전부 DB 인용 (fallback 겸 프롬프트 재료)."""
    counts: dict = {}
    for it in per_wafer:
        counts[it.get("recommendation")] = counts.get(it.get("recommendation"), 0) + 1
    mix = " · ".join(f"{k} {v}장" for k, v in sorted(counts.items()))
    lines = [f"대상 {len(per_wafer)}장 — 엔지니어 선택: {mix}"]
    pr = _pred_range(facts)
    if pr:
        lines.append(f"예측 C65 {pr[0]:,.1f}~{pr[1]:,.1f} (A 파이프라인 · 이 웨이퍼들의 값)"
                     if pr[0] != pr[1] else f"예측 C65 {pr[0]:,.1f} (A 파이프라인)")
    if facts.get("sensors"):
        s = facts["sensors"][0]
        top = ", ".join(f"{x['sensor']}({x['rule']}·{x['n']}건)" for x in facts["sensors"][:3])
        lines.append(f"위반 상위: {top}" if s.get("sensor") else "위반 센서 기록 없음")
    elif facts.get("incident"):
        lines.append(f"Incident {facts['incident'].get('type')} · 알람 "
                     f"{facts['incident'].get('alarms')}건 · severity {facts['incident'].get('severity')}")
    if facts.get("inhibit"):
        ih = facts["inhibit"]
        lines.append(f"RTD 정지 이력 {ih['chamber_id']} {ih['inhibited_at']}"
                     + (f" → 해제 {ih['released_at']}" if ih.get("released_at") else " (해제 전)"))
    return lines[:3]


def _facts_block(facts: dict, per_wafer: list) -> str:
    """LLM 에 넘길 사실 블록 (JSON) — 여기 없는 값은 쓰지 말라고 프롬프트가 명시한다."""
    sel = {it.get("wafer_id"): it.get("recommendation") for it in per_wafer}
    wafers = []
    for w in facts.get("wafers", []):
        wafers.append({"wafer_id": w.get("wafer_id"), "lot": w.get("lot_id"),
                       "engineer_choice": sel.get(w.get("wafer_id")),
                       "agent_recommendation": w.get("recommendation"),
                       "predicted_c65": w.get("predicted_c65"),
                       "actual_c65": w.get("actual_c65"),
                       "anomaly_score": w.get("anomaly"),
                       "hold_reason": w.get("hold_reason"),
                       "basis": w.get("basis")})
    for wid, ch in sel.items():                        # 원장 행이 아직 없는 선택도 사실로 넘긴다
        if not any(x["wafer_id"] == wid for x in wafers):
            wafers.append({"wafer_id": wid, "engineer_choice": ch})
    return json.dumps({"incident": facts.get("incident"), "chamber": facts.get("chamber_id"),
                       "wafers": wafers, "top_violations": facts.get("sensors"),
                       "rtd_inhibit": facts.get("inhibit")},
                      ensure_ascii=False, indent=1)


def build_messages(facts: dict, per_wafer: list) -> list:
    """chat messages 조립 — system(규칙) + user(사실 + 요구 형식)."""
    user = (
        "다음은 한 Incident 의 웨이퍼 처분 심사 자료다. 엔지니어가 각 웨이퍼에 대해 "
        "RELEASE(정상 투입) 또는 SCRAP(폐기)을 이미 선택했다.\n\n"
        f"[사실]\n{_facts_block(facts, per_wafer)}\n\n"
        "요구 출력(JSON):\n"
        "· reason: 이 처분 결정을 2~3문장으로 요약. 엔지니어 선택과 에이전트 권고가 다르면 "
        "그 차이를 명시한다.\n"
        "· evidence: 근거 2~3개. 각 한 문장, 사실의 수치를 그대로 인용.\n"
        "· counter_evidence: 이 처분에서 놓칠 수 있는 위험 한 문장 (SCRAP 은 비가역, "
        "RELEASE 는 후속 불량 유출 가능성 관점).\n"
        "· confidence: 0~1.\n"
    )
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# ③ LLM 호출 (vLLM OpenAI 호환) — 표준 라이브러리만 사용
# ---------------------------------------------------------------------------
def _llm_cfg(params: dict) -> dict:
    """params.yaml llm.* + .env 엔드포인트 → 호출 설정. 하드코딩 금지(6-1)."""
    llm = (params or {}).get("llm", {}) or {}
    mode = (os.getenv("AGENT_LLM_MODE") or "api").strip().lower()
    # gen_model = Ollama 태그 / vllm_model = HF repo id — 엔진마다 식별자 체계가 다르다(D9-b).
    model = llm.get("vllm_model") if mode == "api" else llm.get("gen_model")
    return {"mode": mode, "model": model,
            "base_url": (os.getenv("AGENT_LLM_BASE_URL") or "").rstrip("/"),
            "temperature": llm.get("temperature"), "seed": llm.get("seed"),
            "max_tokens": llm.get("max_output_tokens"),
            "timeout": float(llm.get("call_timeout_sec") or 20),
            "retry": int(llm.get("call_retry") or 1)}


def _post_chat(cfg: dict, messages: list) -> str:
    """POST {base}/v1/chat/completions → content 문자열. 예외는 호출측이 fallback 처리."""
    body: dict[str, Any] = {"model": cfg["model"], "messages": messages}
    if cfg.get("temperature") is not None:
        body["temperature"] = cfg["temperature"]
    if cfg.get("seed") is not None:
        body["seed"] = cfg["seed"]                     # D12 시연 재현성
    if cfg.get("max_tokens") is not None:
        body["max_tokens"] = cfg["max_tokens"]
    if cfg["mode"] == "api":
        body["guided_json"] = BRIEF_SCHEMA             # vLLM 확장 — 스키마 토큰 레벨 강제
        # qwen3.6 은 thinking 모델 — 억제 안 하면 사고과정에 토큰을 다 쓰고 content 가 빈다(실측).
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(
        f"{cfg['base_url']}/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:   # noqa: S310 — 내부 엔드포인트
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _extract_json(text: str) -> dict:
    """응답에서 JSON 오브젝트 추출 — guided_json 이면 그대로, 아니면 첫 {…} 블록."""
    t = (text or "").strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception:                                  # noqa: BLE001 — 코드펜스·산문 혼입 대비
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        obj = json.loads(t[i:j + 1])
        if isinstance(obj, dict):
            return obj
    raise ValueError("응답에서 JSON 오브젝트를 찾지 못함")


def _fallback(facts: dict, per_wafer: list, why: str) -> dict:
    """LLM 실패 — 결정적 근거로 카드를 세운다 (헌법 6-3, conf 0.00 정직 표기)."""
    log.warning("처분 brief LLM 실패 → fallback: %s", why)
    return {"verdict": "wafer_disposition",
            "reason": f"LLM 분석 실패 — 수동 검토 요청 ({why}). 아래 근거는 원장·예측 실값 인용.",
            "confidence": 0.0,
            "evidence": deterministic_lines(facts, per_wafer),
            "counter_evidence": "SCRAP 은 비가역이다 — 근거 산문 없이 확정하려면 엔지니어가 "
                                "웨이퍼별 값을 직접 확인해야 한다.",
            "llm": {"ok": False, "why": why}}


def build_brief(facts: dict, per_wafer: list, params: dict) -> dict:
    """처분 Brief 1건 — 실패 시 fallback (항상 카드가 세워질 형태를 돌려준다)."""
    cfg = _llm_cfg(params)
    if not cfg["base_url"] or not cfg["model"]:
        return _fallback(facts, per_wafer, "엔드포인트/모델 미설정 (AGENT_LLM_BASE_URL·llm.*)")
    messages = build_messages(facts, per_wafer)
    last = ""
    for attempt in range(max(1, cfg["retry"] + 1)):    # call_retry(D14) — 1회 재시도 기본
        t0 = time.monotonic()
        try:
            raw = _post_chat(cfg, messages)
            obj = _extract_json(raw)
            ev = [str(x) for x in (obj.get("evidence") or []) if str(x).strip()]
            if not ev:
                raise ValueError("evidence 비어 있음")
            conf = obj.get("confidence")
            conf = float(conf) if isinstance(conf, (int, float)) else 0.0
            ms = int((time.monotonic() - t0) * 1000)
            log.info("🧠 처분 brief 생성 — %s (%dms · conf %.2f · 근거 %d줄)",
                     cfg["model"], ms, conf, len(ev))
            return {"verdict": "wafer_disposition",
                    "reason": str(obj.get("reason") or "").strip() or "—",
                    "confidence": max(0.0, min(1.0, conf)),
                    "evidence": ev[:3],
                    "counter_evidence": str(obj.get("counter_evidence") or "—").strip(),
                    "llm": {"ok": True, "model": cfg["model"], "latency_ms": ms,
                            "mode": cfg["mode"], "attempt": attempt + 1}}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"호출 실패 {type(e).__name__}"
        except Exception as e:                         # noqa: BLE001 — 파싱·스키마 위반
            last = f"파싱 실패 {type(e).__name__}: {str(e)[:80]}"
    return _fallback(facts, per_wafer, last or "미상")


# ---------------------------------------------------------------------------
# ④ 카드 report 조립 — 승인 그래프/화면이 읽는 형태
# ---------------------------------------------------------------------------
def build_report(facts: dict, per_wafer: list, brief: dict) -> dict:
    """`start_disposition` 에 넘길 report (= approval_records.original_value).

    S4 는 `original_value.brief.supervisor_recommendation` 을 읽어 02 패널을 그리고,
    `wafer_disposition.per_wafer` 로 03 패널(웨이퍼별 선택)을 그린다. 확정 SQL 도 같은
    per_wafer 를 쓴다 — 화면이 본 목록과 원장에 박히는 목록이 **같은 배열**이다.
    """
    counts: dict = {}
    for it in per_wafer:
        counts[it.get("recommendation")] = counts.get(it.get("recommendation"), 0) + 1
    return {
        "request_type": "disposition",
        "selected_option": "disposition_option",
        "verdict": "wafer_disposition",
        "chamber_id": facts.get("chamber_id"),
        "brief": {
            "supervisor_recommendation": {
                "selected": "disposition_option",
                "verdict": "wafer_disposition",
                "reason": brief.get("reason"),
                "confidence": brief.get("confidence"),
                "evidence": brief.get("evidence"),
                "counter_evidence": brief.get("counter_evidence"),
            },
            "parallel_options": {},                    # 처분 카드는 4지선다가 없다 (03 = 웨이퍼 목록)
        },
        "wafer_disposition": {"per_wafer": per_wafer, "counts": counts},
        "facts": {"sensors": facts.get("sensors"), "inhibit": facts.get("inhibit"),
                  "pred_range": _pred_range(facts)},
        "llm": brief.get("llm"),
    }
