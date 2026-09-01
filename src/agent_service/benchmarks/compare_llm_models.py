# -*- coding: utf-8 -*-
"""
LLM 생성 모델 비교 하네스 (반자동) — gen_model 후보 3종 중 1종 선택용.

임베딩(compare_embed_models.py)은 hit@k로 완전 자동 채점하지만, LLM 생성 품질은
정성 요소가 있어 반자동으로 본다:
  · 자동 채점 : 판정정확도(4지선다 골든셋 12문항 정답 대조) · JSON 유효율 · 지연시간
  · 사람 채점 : 리포트/추론 출력을 3열 덤프해 눈+루브릭으로 비교
  ※ 지연시간은 모델별 워밍업(프리로드) 후 측정 → 첫 프롬프트 로딩 지연 제외(순수 추론 지연).

대상 후보 (params.yaml D9 gen_model): Qwen3-30B-A3B Q4 vs EXAONE 32B (재대결 — Gemma 탈락).
서빙은 로컬 Ollama(외부 API 미사용 · params D 방침). 모델명은 Ollama 태그로 지정.

실행 (EC2에서 후보 pull 후):
  python -m src.agent_service.compare_llm_models \\
    --models qwen3:30b-a3b,exaone3.5:32b \\
    --report /opt/agent_service/llm_rematch_report.md \\
    --dump   /opt/agent_service/llm_rematch_results.json
  # 사전: ollama pull <각 모델>  (VRAM 순차 — 30B Q4≈18~22GB, g5 24GB에 하나씩)
"""
import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ [llm-cmp] %(message)s")
log = logging.getLogger("compare-llm")

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
TEMPERATURE = 0.15          # params.yaml D10
JSON_RETRY = 2              # params.yaml D3 (헌법 6-3)
MAX_TOKENS = 800            # 출력 상한(num_predict) — 지연 바운드 (리포트 6~10문장이면 충분)
TIMEOUT = 400               # 30B 첫 로딩(18GB→VRAM)+생성 여유. 프리워밍하면 이후는 훨씬 빠름

# ── 프롬프트 세트 (self-contained — Qdrant 불필요, 컨텍스트 인라인) ──────────────
# 도메인: Etch FDC. 센서는 C코드(헌법 6-4). 4지선다 프레임 = 헌법 1-4.
_JSON_INSTRUCT = (
    "반드시 아래 JSON 스키마로만 응답하세요. 코드블록·설명 없이 JSON 객체 하나만 출력합니다.\n"
    '{"root_cause": "<진짜 장비 이상|공정 조건 이탈|기준선 노후|모두 아님>", '
    '"action": "<정비+스크랩|Recipe R2R 튜닝안|실력치 재설정|공정 검토 에스컬레이션>", '
    '"reason": "<한 문장 근거>", "confidence": <0.0~1.0>}'
)

def _judge(id_: str, root_cause: str, action: str, ctx: str) -> dict:
    """4지선다 판정 골든 케이스 1건. expect = 정답(root_cause·action) → 자동 채점."""
    return {"id": id_, "category": "4지선다판정", "json": True,
            "keys": ["root_cause", "action", "reason", "confidence"],
            "expect": {"root_cause": root_cause, "action": action},
            "prompt": ctx + "\n판정하시오.\n" + _JSON_INSTRUCT}


# ── 판정 골든셋 12문항 (4지선다 4범주 × 3, 정답 라벨 부여 — qwen vs exaone 재대결 지표) ──
#   범주↔조치 1:1 (헌법 1-4): 진짜 장비이상→정비+스크랩 · 공정 조건 이탈→Recipe R2R · 기준선 노후→실력치 재설정 · 모두 아님→에스컬레이션
_JUDGE_CASES = [
    # ── 범주 1: 진짜 장비 이상 → 정비+스크랩 (급변·고장 시그니처·정비 precedent) ──
    _judge("judge_eq_rfmatch", "진짜 장비 이상", "정비+스크랩",
           "Incident INC-20250614-CH01: C32(RF reflected/매칭 지표) 급변, C31(RF 계열) 동반 이상. SHAP top3=C32,C31,C11. "
           "최근 정비 이력 없음. 유사 과거사례 3건 모두 'match network 정비'로 해결(성공률 100%). PM 근접도 낮음(사이클 초반)."),
    _judge("judge_eq_valve", "진짜 장비 이상", "정비+스크랩",
           "Incident INC-20250705-CH04: C57(밸브/유량 제어 설정 축)이 특정 개도에서 고착, 압력 제어 불능. C58(He backside) 동반 이상. "
           "N1(급변) 트리거, severity critical. 밸브 구동부 이상 의심, error_manual 'throttle valve 구동부 점검/교체'. 웨이퍼 5장 영향."),
    _judge("judge_eq_chuck", "진짜 장비 이상", "정비+스크랩",
           "Incident INC-20250811-CH02: C17(척 온도)이 setpoint 대비 계단형 급락 후 회복 안 됨. C9(열 거동) 동반. ESC/히터 냉각계통 이상 신호. "
           "SHAP top3=C17,C9,C58. 정비 이력과 강한 상관, 유사사례 'ESC He line/heater 점검' 정비로 해결. 다중 wafer 영향."),
    # ── 범주 2: 공정 조건 이탈 → Recipe R2R 튜닝안 (목표 이탈·장비 정상·튜닝 손잡이 존재) ──
    _judge("judge_rc_bias", "공정 조건 이탈", "Recipe R2R 튜닝안",
           "Incident INC-20250808-CH02: C11(DC self-bias)이 목표 대비 이탈, C65(예측 타겟) 상승 경향. 장비 자체 이상징후(정비 신호) 없음. "
           "SHAP: C11 기여 지배적. Process KB: 'DC bias는 RF power와 -0.978 연동 → power 하향이 bias 완화'. 유사 recipe 튜닝 사례 2건 검증 성공."),
    _judge("judge_rc_gas", "공정 조건 이탈", "Recipe R2R 튜닝안",
           "Incident INC-20250820-CH05: C48(Main Gas Flow)이 목표 대비 지속 편의, C65 영향. MFC 자체는 정상(장비 이상 신호 없음). "
           "SHAP top3=C48,C16,C15. Process KB: 'gas 유량 setpoint(C48) 튜닝으로 정상 범위 복귀'. 유사 gas setpoint 튜닝 사례 검증 성공."),
    _judge("judge_rc_pressure", "공정 조건 이탈", "Recipe R2R 튜닝안",
           "Incident INC-20250902-CH03: C57 연동 압력 지표가 공정 조건에서 이탈. 밸브는 정상 작동(고착 아님, 구동부 정상). C65 소폭 상승. "
           "SHAP: C57 지배. Process KB: 'pressure setpoint 튜닝으로 압력 정상화'. 유사 pressure setpoint 튜닝 precedent 존재."),
    # ── 범주 3: 기준선 노후 → 실력치 재설정 (완만 드리프트·N2·정기 리캘·무이상) ──
    _judge("judge_bl_gasflow", "기준선 노후", "실력치 재설정",
           "Incident INC-20250701-CH03: C48(Main Gas Flow 실측)이 수 사이클에 걸쳐 완만히 드리프트. Nelson 위반은 N2(경향)만. "
           "무이상 정기 리캘리브레이션 주기 도래. SHAP top3=C48,C16,C9. 유사사례: rolling mean±3σ 실력치 재설정 4건."),
    _judge("judge_bl_vdc", "기준선 노후", "실력치 재설정",
           "Incident INC-20250715-CH01: C11 관리한계가 노후 — 최근 100표본 rolling mean이 기존 center에서 벗어남. 이상 징후 없음(장비·공정 정상). "
           "정기 리캘리브레이션 대상, 변동폭 상한 이내. SPC 위반은 N2(경향)뿐. 유사사례: 실력치 center 재산정."),
    _judge("judge_bl_drift", "기준선 노후", "실력치 재설정",
           "Incident INC-20250728-CH06: C25(장기 baseline drift 지표)가 장기적으로 천천히 이동, PM 후에도 유지. SPC 급변 위반은 없음. "
           "관리한계가 현 분포와 어긋남(노후). SHAP top3=C25,C11,C63. 유사사례: rolling mean±3σ 기준선 재설정."),
    # ── 범주 4: 모두 아님 → 공정 검토 에스컬레이션 (재발·다중챔버 공통·신호 상충) ──
    _judge("judge_esc_reopen", "모두 아님", "공정 검토 에스컬레이션",
           "Incident INC-20250905-CH02: 동일 패턴이 3회 재발(reopen). 이전에 정비·recipe 튜닝·실력치 재설정을 모두 시도했으나 미개선. "
           "근본원인 불명, 단일 조치로 설명 안 됨. SHAP 불안정(사이클마다 top 센서 바뀜). 공정팀 심층 검토 필요."),
    _judge("judge_esc_multi", "모두 아님", "공정 검토 에스컬레이션",
           "Incident INC-20250912: 여러 챔버(CH01/CH03/CH05)에서 동시에 유사 이탈 관측. 단일 장비 이상으로 설명 불가(공통 원인 의심). "
           "multi_chamber_common 신호. 정비/튜닝/실력치 어느 것도 단독 근거 부족. 참조 챔버 대비 전반적 shift."),
    _judge("judge_esc_conflict", "모두 아님", "공정 검토 에스컬레이션",
           "Incident INC-20250920-CH04: 신호 상충 — SHAP는 C11 지목하나 정비 이력 근거도 recipe 튜닝 근거도 약함. 유사 과거사례 없음. "
           "confidence 낮음, N7(중심 밀집)만. 어느 4지선다도 근거 불충분 → 사람 검토로 넘겨야."),
]

PROMPTS = _JUDGE_CASES + [
    # ── B. 리포트 생성 (한국어 서술 품질) ──
    {"id": "report_maintenance", "category": "리포트생성", "json": False,
     "prompt": "다음 근거로 엔지니어용 정비 권고 리포트를 한국어로 작성하시오(6~10문장). "
               "근거: 챔버 CH01, C32 RF 매칭 지표 급변+C31 동반, error_manual 'match network 정비'(부품: RF matching capacitor, 조치: 매칭 재조정/캐패시터 점검), "
               "severity critical, 웨이퍼 3장 영향. 승인 대기 상태. 발표용 센서명 쓰지 말고 C코드 유지."},
    {"id": "report_limit", "category": "리포트생성", "json": False,
     "prompt": "다음 근거로 실력치(관리한계) 재설정 브리프를 한국어로 작성하시오(6~10문장). "
               "근거: C48 최근 50표본 rolling mean±3σ 재산정, center 1576→1560(-1.0%), trigger_type=periodic(무이상), "
               "변동폭 상한 이내, limit v3→v4. 섀도 평가 가성알람 12% 감소. 자동 적용 예외 조건 충족(헌법 1-1) 여부를 명시."},
    # ── C. RAG 근거 요약 (JSON 구조화) ──
    {"id": "rag_cite", "category": "RAG추론", "json": True, "keys": ["summary", "citations"],
     "prompt": "검색된 KB 카드 3건으로 근거를 요약하고 인용을 구조화하시오.\n"
               "[1] MAN-OX100-0137 error_manual: 'Vacuum pump 과부하/단락 시 process gas 즉시 차단, 인터록 status fault'.\n"
               "[2] MAN-OX100-0065 error_manual: 'Interlock open circuit 시 전 power output 비활성(Emergency Off/전원 차단/외부 인터록)'.\n"
               "[3] PK-SENSOR-C11 process_knowledge: 'DC self-bias, RF 전력과 corr -0.978'.\n"
               "질의: '진공 펌프 인터록 fault 시 대응 근거'.\n"
               '반드시 JSON만: {"summary": "<2~3문장 요약>", "citations": ["<관련 id>", ...]}'},
]


def _post_chat(body: dict) -> str:
    req = urllib.request.Request(f"{OLLAMA_HOST}/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())["message"]["content"]


def ollama_generate(model: str, prompt: str, want_json: bool) -> str:
    """Ollama /api/chat 1회 호출.
    think=false 로 추론모드(Qwen3 등) 비활성 — 추론 토큰 남발로 인한 지연·JSON오염 방지.
    thinking 미지원 모델(gemma2·exaone 등)은 think 빼고 폴백. want_json 이면 format=json 강제."""
    body = {
        "model": model, "messages": [{"role": "user", "content": prompt}],
        "stream": False, "think": False, "keep_alive": "10m",   # 프롬프트 사이 모델 상주 유지
        "options": {"temperature": TEMPERATURE, "num_predict": MAX_TOKENS},
    }
    if want_json:
        body["format"] = "json"
    try:
        return _post_chat(body)
    except urllib.error.HTTPError as e:
        if e.code == 400 and "think" in body:   # thinking 미지원 모델 → think 제거 후 재시도
            body.pop("think")
            return _post_chat(body)
        raise


def _warmup(model: str) -> float:
    """모델을 VRAM에 프리로드 — 첫 프롬프트의 로딩 지연을 측정에서 제외한다.
    생성은 최소화(num_predict=1). 소요 시간(로딩) 반환(참고용, 지표엔 미포함)."""
    t0 = time.time()
    body = {"model": model, "messages": [{"role": "user", "content": "ping"}],
            "stream": False, "think": False, "keep_alive": "10m", "options": {"num_predict": 1}}
    try:
        _post_chat(body)
    except urllib.error.HTTPError as e:
        if e.code == 400 and "think" in body:      # thinking 미지원 모델 폴백
            body.pop("think")
            try:
                _post_chat(body)
            except Exception as e2:
                log.warning(f"{model} 워밍업 실패(무시): {e2}")
    except Exception as e:
        log.warning(f"{model} 워밍업 실패(무시): {e}")
    return round(time.time() - t0, 1)


def _norm(s) -> str:
    """4지선다 라벨 대조용 정규화 — 공백 제거(예: '진짜 장비 이상'=='진짜장비이상')."""
    return "".join(str(s).split()) if s is not None else ""


def _run_one(model: str, p: dict) -> dict:
    """모델×프롬프트 1건 실행 → {text, latency_s, json_valid, 판정채점}. 헌법 6-3: JSON 재시도·폴백."""
    want_json = p.get("json", False)
    expect = p.get("expect")
    t0 = time.time()
    text, json_valid = "", None
    root_cause_ok = action_ok = pred_rc = pred_ac = None
    for attempt in range(1, (JSON_RETRY + 2) if want_json else 2):
        try:
            text = ollama_generate(model, p["prompt"], want_json)
            if not want_json:
                break
            data = json.loads(text)                       # 파싱 성공?
            keys = p.get("keys", [])
            json_valid = all(k in data for k in keys)     # 필수 키 존재?
            if expect:                                    # 판정 골든셋 → 정답 대조 자동 채점
                pred_rc, pred_ac = data.get("root_cause"), data.get("action")
                root_cause_ok = _norm(pred_rc) == _norm(expect["root_cause"])
                action_ok = _norm(pred_ac) == _norm(expect["action"])
            break
        except json.JSONDecodeError:
            json_valid = False
            if expect:
                root_cause_ok = action_ok = False         # 파싱 실패 = 판정 실패로 계상
            log.warning(f"{model} {p['id']}: JSON 파싱 실패(시도 {attempt})")
        except Exception as e:
            log.warning(f"{model} {p['id']}: 호출 실패 — {e}")
            break
    return {"text": text, "latency_s": round(time.time() - t0, 2), "json_valid": json_valid,
            "root_cause_ok": root_cause_ok, "action_ok": action_ok,
            "pred_root_cause": pred_rc, "pred_action": pred_ac}


def evaluate(models: list) -> dict:
    """모델별 전 프롬프트 실행. {model: {prompt_id: result}} 반환.
    각 모델은 측정 전 워밍업(프리로드)해 로딩 지연을 latency 에서 제외 — 순수 추론 지연만 집계."""
    out = {}
    for m in models:
        load_s = _warmup(m)                       # 첫 프롬프트 로딩 지연 제외
        log.info(f"=== 모델: {m} (워밍업 {load_s}s — 지표 제외) ===")
        out[m] = {}
        for p in PROMPTS:
            r = _run_one(m, p)
            out[m][p["id"]] = r
            vtag = "" if r["json_valid"] is None else (" JSON✓" if r["json_valid"] else " JSON✗")
            log.info(f"  {p['id']:22s} {r['latency_s']:6.1f}s{vtag}")
    return out


def _agg(model_res: dict) -> dict:
    """모델 1개 집계 — 판정정확도(골든셋), 평균지연, JSON유효율, slow-path 초과(30s)."""
    lat = [r["latency_s"] for r in model_res.values()]
    js = [r["json_valid"] for r in model_res.values() if r["json_valid"] is not None]
    over = sum(1 for x in lat if x > 30)   # params D4 slow_path 30s
    rc = [r["root_cause_ok"] for r in model_res.values() if r.get("root_cause_ok") is not None]
    ac = [r["action_ok"] for r in model_res.values() if r.get("action_ok") is not None]
    return {
        "judge_n": len(rc),
        "judge_root_cause_acc": round(sum(rc) / len(rc), 3) if rc else None,   # 재대결 핵심 지표
        "judge_action_acc": round(sum(ac) / len(ac), 3) if ac else None,
        "avg_latency_s": round(sum(lat) / len(lat), 1) if lat else 0.0,
        "json_valid_rate": round(sum(js) / len(js), 2) if js else None,
        "slowpath_over_30s": over,
    }


def build_report(models: list, results: dict) -> str:
    """자동지표 표 + 프롬프트별 3열 출력 덤프(사람 채점용) 마크다운."""
    n_judge = sum(1 for p in PROMPTS if p.get("expect"))
    lines = ["# LLM 모델 비교 리포트 (반자동)\n",
             f"- 후보 {len(models)}종 · 프롬프트 {len(PROMPTS)}개(판정 골든셋 {n_judge}) · temp={TEMPERATURE}\n",
             "> **판정정확도(root_cause)** = 4지선다 골든셋 자동 채점(재대결 핵심). 리포트/RAG 품질은 아래 덤프를 눈으로 비교.\n",
             "## 자동 지표\n",
             "| 모델 | 판정정확도 | (action) | 평균지연(s) | JSON유효율 | 30s초과 |",
             "|---|---|---|---|---|---|"]
    for m in models:
        a = _agg(results[m])
        rc = "-" if a["judge_root_cause_acc"] is None else f"{a['judge_root_cause_acc']:.2f} ({int(a['judge_root_cause_acc']*a['judge_n'])}/{a['judge_n']})"
        ac = "-" if a["judge_action_acc"] is None else f"{a['judge_action_acc']:.2f}"
        jr = "-" if a["json_valid_rate"] is None else f"{a['json_valid_rate']:.2f}"
        lines.append(f"| {m} | {rc} | {ac} | {a['avg_latency_s']} | {jr} | {a['slowpath_over_30s']} |")
    lines.append("\n## 판정 골든셋 상세 (정답 vs 모델 픽)\n")
    lines.append("| 판정 케이스 | 정답 root_cause | " + " | ".join(models) + " |")
    lines.append("|---|---|" + "|".join(["---"] * len(models)) + "|")
    for p in PROMPTS:
        if not p.get("expect"):
            continue
        cells = []
        for m in models:
            r = results[m][p["id"]]
            mark = "✅" if r.get("root_cause_ok") else "❌"
            cells.append(f"{mark} {r.get('pred_root_cause') or '-'}")
        lines.append(f"| {p['id']} | {p['expect']['root_cause']} | " + " | ".join(cells) + " |")
    lines.append("\n## 프롬프트별 출력 (사람 채점용)\n")
    for p in PROMPTS:
        lines.append(f"### [{p['category']}] {p['id']}\n")
        if p.get("expect"):
            lines.append(f"_정답: {p['expect']['root_cause']} / {p['expect']['action']}_\n")
        for m in models:
            r = results[m][p["id"]]
            vtag = "" if r["json_valid"] is None else (" · JSON✓" if r["json_valid"] else " · JSON✗")
            lines.append(f"**{m}** ({r['latency_s']}s{vtag})\n```\n{(r['text'] or '').strip()[:1200]}\n```\n")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True, help="Ollama 태그 쉼표 구분 (예: qwen3:30b-a3b,gemma2:27b,exaone3.5:32b)")
    ap.add_argument("--report", default="llm_compare_report.md")
    ap.add_argument("--dump", default="llm_compare_results.json")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    results = evaluate(models)

    Path(args.report).write_text(build_report(models, results), encoding="utf-8")
    Path(args.dump).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{'모델':<24}{'판정정확도':>12}{'평균지연':>10}{'JSON유효':>10}{'30s초과':>9}")
    for m in models:
        a = _agg(results[m])
        rc = "-" if a["judge_root_cause_acc"] is None else f"{a['judge_root_cause_acc']:.2f}({int(a['judge_root_cause_acc']*a['judge_n'])}/{a['judge_n']})"
        jr = "-" if a["json_valid_rate"] is None else f"{a['json_valid_rate']:.2f}"
        print(f"{m:<24}{rc:>12}{a['avg_latency_s']:>9}s{jr:>10}{a['slowpath_over_30s']:>9}")
    print(f"\n리포트: {args.report} · 덤프: {args.dump}")


if __name__ == "__main__":
    main()
