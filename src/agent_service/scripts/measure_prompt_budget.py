# -*- coding: utf-8 -*-
"""프롬프트 토큰 예산 측정 — vLLM max_model_len 초과를 **EC2 없이** 잡는다.

왜 있나 (2026-07-28):
  recipe tool 이 400 Bad Request 로 100% fallback 된 사고가 **세 번째**였다.
    · 2026-07-21 round: recipe 18/25 fallback
    · 2026-07-23 fewshot2: **41/41** (정답률 38.9% — few-shot 예시 늘린 실험)
    · 2026-07-28 round9: **42/42** (7/27 온도 규칙4-1 + few-shot 예시 C 추가분)
  세 번 다 **EC2 를 켜고 47건을 다 돌린 뒤에야** 알았다. 400 은 조용해서
  "측정 불가"가 "나쁜 성적"으로 읽힌다(라운드7 교훈과 같은 계열).
  프롬프트 길이는 LLM 없이 로컬에서 잴 수 있다 — 그래서 여기서 잰다.

무엇을 재나:
  build_messages(SYSTEM_BLOCK_V1 + tool 프롬프트 + JSON지시/예시) + user_payload
  = 실제로 vLLM 에 가는 messages 전체. fixture 전건 × tool 3종.

토큰 환산:
  tokenizer 가 있으면(transformers + 모델 토크나이저) 실측, 없으면 문자수 기반 추정.
  추정식은 보수적으로 잡는다 — 과소추정하면 이 스크립트가 통과시켜버려 의미가 없다.

사용:
  python -m src.agent_service.scripts.measure_prompt_budget
  python -m src.agent_service.scripts.measure_prompt_budget --max-model-len 16384 --reserve 2048
  python -m src.agent_service.scripts.measure_prompt_budget --tool recipe --verbose
종료코드: 예산 초과 fixture 가 하나라도 있으면 1 (CI 게이트로 사용 가능).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_service.app.config import FIXTURES_DIR, load_settings  # noqa: E402
from agent_service.app.llm.prompts import build_messages  # noqa: E402
from agent_service.app.schemas.alert import AlertModel  # noqa: E402
from agent_service.app.tools import limit as limit_tool  # noqa: E402
from agent_service.app.tools import maintenance as maint_tool  # noqa: E402
from agent_service.app.tools import recipe as recipe_tool  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("prompt_budget")

# ⚠️ max_model_len 은 **프롬프트 + 생성** 합계다. 출력 여유를 빼야 실제 프롬프트 예산이 나온다.
#
# 🔴 **기본 상수를 두지 않는다** (2026-08-04). 구 코드는 `DEFAULT_MAX_MODEL_LEN = 8192` 였는데
#    서버는 2026-07-28 에 12288 로 올라갔고(variables.tf) 이 상수만 8192 에 멈춰 있었다 —
#    그 결과 recipe 46건 중 38건을 **"예산 초과"로 오탐**했다. 오탐이 위험한 이유는 방향 때문이다:
#      · 과소 추정(8192←12288) → 오탐 → 사람이 **재료를 줄이는** 잘못된 조치(근거가 얇아진다)
#      · 과대 추정(12288←8192) → 미탐 → **400 사고 재발**. 이 스크립트가 막으려던 바로 그것
#    상수를 12288 로 갱신해도 언젠가 같은 자리에서 또 낡는다. 그래서 값을 박지 않고
#    **서버 실제값(.env `AGENT_LLM_MAX_MODEL_LEN`)을 읽고, 없으면 측정하지 않는다.**
#    같은 판단을 `app/llm/client._max_model_len` 이 2026-07-29 에 먼저 내렸다("폴백을 두지 않는다") —
#    여기서도 그 함수를 그대로 재사용해 두 곳이 갈라지지 않게 한다.
DEFAULT_RESERVE = 2048          # 생성(출력) 여유 — 리포트 JSON 이 그만큼 나온다고 보고 잡는다

# vLLM 이 쓰는 모델 (infra/llm-ec2-experiment/variables.tf vllm_model 기본값).
# 토크나이저만 받으므로 다운로드는 수 MB — 가중치를 받지 않는다.
DEFAULT_TOKENIZER = "QuantTrio/Qwen3.6-35B-A3B-AWQ"

# 문자→토큰 환산 (토크나이저 없을 때만). **2026-07-28 실측으로 보정**:
#   추정 1.6 자/토큰은 과대추정이라 maintenance 를 46/46 초과로 오탐했다(실제는 통과).
#   실측 역산: limit 2.27 · maintenance 2.49 · recipe 2.23 자/토큰
#   → **가장 작은 값(2.2)** 을 쓴다. 토큰을 크게 잡는 쪽이 안전 —
#     과소추정하면 이 스크립트가 초과를 통과시켜 존재 이유가 사라진다.
CHARS_PER_TOKEN_FALLBACK = 2.2


def _load_tokenizer(model: Optional[str]):
    """transformers 토크나이저 로드 시도. 실패하면 None (문자수 추정으로 폴백)."""
    if not model:
        return None
    try:
        from transformers import AutoTokenizer  # 지연 import — 없으면 추정 경로
        return AutoTokenizer.from_pretrained(model, trust_remote_code=True)
    except Exception as exc:  # noqa: BLE001 — 토크나이저는 있으면 좋은 것, 없어도 진행
        log.warning("토크나이저 로드 실패(문자수 추정으로 진행): %s", exc)
        return None


def _count(text: str, tok) -> int:
    """토큰 수 — 토크나이저가 있으면 실측, 없으면 보수적 추정."""
    if tok is not None:
        return len(tok.encode(text, add_special_tokens=False))
    return int(len(text) / CHARS_PER_TOKEN_FALLBACK + 0.5)


def _messages_text(messages: list[dict[str, str]]) -> str:
    """messages 를 하나의 문자열로 — chat template 오버헤드는 무시(수십 토큰)."""
    return "\n".join(m.get("content", "") for m in messages)


# --- tool 별 payload 조립 --------------------------------------------------------
# 각 tool 의 run() 이 실제로 만드는 payload 를 **같은 함수로** 만든다. 재료 배분도 pipeline 의
# _tool_kwargs 를 그대로 써서 어긋남을 없앤다 — 여기서 재현이 틀리면 측정이 무의미하다.
def _recipe_case(alert: AlertModel, kw: dict[str, Any]) -> tuple[str, str, str]:
    """recipe tool 1건분 프롬프트 3조각 → (시스템, payload, 스키마 예시).

    `tuning` 은 stub API 로 만든다 — 실측 대상은 B 엔진 값이 아니라 **프롬프트 길이**라
    수치의 정확도보다 필드 구성이 운영과 같은지가 중요하다.
    """
    payload = recipe_tool.build_payload(
        alert, tuning=recipe_tool._stub_tuning_api(alert),
        knob_map=kw.get("knob_map"), kb_hits=kw.get("kb_hits"),
        cases=kw.get("cases"), recipe_logs=kw.get("recipe_logs"),
    )
    return recipe_tool.SYSTEM_PROMPT, payload, recipe_tool.SCHEMA_EXAMPLE


def _limit_case(alert: AlertModel, kw: dict[str, Any]) -> tuple[str, str, str]:
    """limit tool 1건분 프롬프트 3조각 → (시스템, payload, 스키마 예시)."""
    # limit 은 kb_hits 를 받지 않는다(§2). recalc 는 tool 자체 조회 — 실측에선 B stub 이라 None.
    payload = limit_tool.build_payload(
        alert, recalc=None, cases=kw.get("cases"), limit_logs=kw.get("limit_logs"),
    )
    return limit_tool.SYSTEM_PROMPT, payload, limit_tool.SCHEMA_EXAMPLE


def _maint_case(alert: AlertModel, kw: dict[str, Any]) -> tuple[str, str, str]:
    """maintenance tool 1건분 프롬프트 3조각 → (시스템, payload, 스키마 예시)."""
    payload = maint_tool.build_payload(
        alert, manual_hits=kw.get("manual_hits"), cases=kw.get("cases"),
    )
    return maint_tool.SYSTEM_PROMPT, payload, maint_tool.SCHEMA_EXAMPLE


def _release_case(alert: AlertModel, kw: dict[str, Any]) -> tuple[str, str, str]:
    """해제 근거 Brief 1건분 프롬프트 3조각 → (시스템, payload, 스키마 예시).

    ⚠️ **다른 셋과 재료 출처가 다르다.** recipe·limit·maintenance 는 alert 에서 나오지만,
    이쪽 재료는 `chamber_inhibits`·`spc_violations` 조회 결과(`collect_materials`)다.
    그래서 alert fixture 를 그대로 못 쓰고 **상한을 꽉 채운 최악 재료**를 합성한다.

    이 함수가 없어서 사고가 났다 (2026-08-10): #154 가 네 번째 LLM 호출을 추가했는데
    `TOOLS` 에 등재하지 않아, 이 스크립트가 **"✅ 전 fixture 예산 내"** 초록불을 내는 동안
    실전에서 ~11,417 토큰으로 vLLM 400 을 받았다. 헌법 7장 *"신설하고 하류를 안 따라감"*.

    합성이 실측을 대신할 수 있는 근거는 **`release_trigger_violation_limit` 상한**이다.
    상한이 생기기 전에는 ⓐ 가 무제한이라 "최악"이 정의되지 않았고, 그래서 잴 수도 없었다.
    상한을 지우면 이 측정도 같이 무의미해진다 — 둘은 세트다.
    """
    from agent_service.app import release_brief as R

    st = load_settings()
    n_viol = int(st.require("agent.release_trigger_violation_limit"))
    n_past = int(st.require("agent.release_past_limit"))
    window_min = int(st.require("agent.release_trend_window_min"))
    # 최악 = 상한 건수 × 각 필드가 실측에서 본 최장 길이. 짧게 잡으면 이 게이트가 과소가 된다.
    materials = {
        "incident_id": "INC-20260810-SIMCH9-999",
        "chamber_id": "SIM_CH_9",
        "chambers": [f"SIM_CH_{i}" for i in range(1, 7)],     # 장비 스코프 = 형제 전체
        "scope": "equipment",
        "equipment_id": "SIM_EQ_1",
        "trigger_alert_id": "ALERT-20260810-SIMCH9-9999",
        "inhibited_at": "2026-08-10T00:00:00+00:00",
        "trigger_violations": [
            {"sensor_id": f"C{60 + i}", "rule_id": "N1", "severity": "CRITICAL",
             "current_value": 1234.5678, "control_limit_upper": 1200.0,
             "control_limit_lower": 800.0,
             "description": f"N{1 + i % 8}: {6 + i}점 창 (C64_{10000 + i}..C64_{10009 + i})"}
            for i in range(n_viol)
        ],
        "persistent_sensors": [
            {"sensor_id": f"C{30 + i}", "breach_ratio": 1.0, "n1_alerts": 170}
            for i in range(8)
        ],
        "window_minutes": window_min,
        "window_total_alerts": 170,
        "past_releases": [
            {"chamber_id": "SIM_CH_9", "incident_id": f"INC-20260801-SIMCH9-{i:03d}",
             "inhibited_at": "2026-08-01T00:00:00+00:00",
             "released_at": "2026-08-01T06:00:00+00:00",
             "released_by": "engineer_hong", "release_qual_id": f"QUAL-20260801-SIMCH9-{i:04d}",
             "scope": "equipment", "downtime_h": 6.0}
            for i in range(n_past)
        ],
    }
    materials["trigger_violations_total"] = n_viol * 3        # 잘렸다고 표시되는 최악
    kb = {"manual_hits": kw.get("manual_hits") or [], "cases": kw.get("cases") or []}
    return R.SYSTEM_PROMPT, R.build_payload(materials, kb), R.SCHEMA_EXAMPLE


TOOLS = {"recipe": _recipe_case, "limit": _limit_case, "maintenance": _maint_case,
         "release": _release_case}


def _tool_inputs(alert: AlertModel) -> dict[str, dict[str, Any]]:
    """pipeline 과 **같은 경로**로 재료를 조회하고 tool 별로 배분한다.

    Qdrant·Postgres 가 떠 있어야 실측이 된다. 조회가 비면 측정값이 **과소**가 되어
    이 스크립트가 초과를 놓치므로, 빈 재료는 경고로 크게 남긴다.
    """
    from agent_service.app import pipeline as P

    recipe_inputs: dict[str, Any] = {}
    manual_inputs: dict[str, Any] = {}
    try:
        recipe_inputs = P._search_recipe_evidence(alert) or {}
        recipe_inputs["knob_map"] = P._load_knob_map()
        recipe_inputs.update(P._fetch_pairing_logs(recipe_inputs.get("cases")) or {})
        manual_inputs = P._search_manual_evidence(alert) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("재료 조회 실패 — 빈 재료로 측정(과소추정!): %s", exc)

    out = {}
    for name in TOOLS:
        tool = type("T", (), {"name": name})()          # _tool_kwargs 는 .name 만 본다
        out[name] = P._tool_kwargs(tool, recipe_inputs, manual_inputs)
    return out


def main() -> int:
    """CLI 진입점 — tool 3종 프롬프트를 조립해 토큰을 재고 예산 초과를 판정한다.

    Returns:
        종료 코드. **예산 초과가 1건이라도 있으면 1** — CI 게이트로 쓰기 위함이다
        (recipe 400 이 세 번 재발한 원인이 "EC2 를 켜고 전수를 돌린 뒤에야 알았다"라서,
        길이 검문을 로컬·CI 로 앞당긴다).
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-model-len", type=int, default=None,
                    help="서버 max_model_len. 생략하면 .env AGENT_LLM_MAX_MODEL_LEN 을 읽는다 "
                         "(기본 상수 없음 — 위 주석 참조)")
    ap.add_argument("--reserve", type=int, default=DEFAULT_RESERVE,
                    help="생성(출력) 예약 토큰 — 프롬프트 예산 = max_model_len − reserve")
    ap.add_argument("--tool", choices=sorted(TOOLS), help="한 tool 만 측정")
    ap.add_argument("--tokenizer", default=DEFAULT_TOKENIZER,
                    help=f"HF 모델명 (기본 {DEFAULT_TOKENIZER}). 토크나이저만 받는다(수 MB)")
    ap.add_argument("--no-tokenizer", action="store_true",
                    help="문자수 추정으로만 (오프라인). 실측보다 부정확 — 게이트 판정은 실측 권장")
    ap.add_argument("--verbose", action="store_true", help="fixture 별 상세")
    args = ap.parse_args()

    # 값의 출처: --max-model-len > .env(AGENT_LLM_MAX_MODEL_LEN). 폴백 상수는 없다(위 주석).
    max_model_len = args.max_model_len
    src = "--max-model-len"
    if max_model_len is None:
        from agent_service.app.llm.client import _max_model_len as _env_mml
        max_model_len, src = _env_mml(), ".env AGENT_LLM_MAX_MODEL_LEN"
    if not max_model_len:
        print("=" * 78)
        print("⛔ max_model_len 을 모른다 — 측정하지 않는다.")
        print("   기준을 모르는 채 통과/실패를 말하면 그 판정이 더 위험하다(오탐→재료 축소 · 미탐→400 재발).")
        print("   해결: .env 에 AGENT_LLM_MAX_MODEL_LEN 설정 (infra variables.tf vllm_max_model_len 과 동일값)")
        print("        확인: curl -s $AGENT_LLM_BASE_URL/v1/models | jq '.data[0].max_model_len'")
        print("        또는: --max-model-len 12288 로 직접 지정")
        print("=" * 78)
        return 2
    budget = max_model_len - args.reserve
    tok = None if args.no_tokenizer else _load_tokenizer(args.tokenizer)
    mode = (f"실측({args.tokenizer})" if tok
            else f"추정(1토큰≈{CHARS_PER_TOKEN_FALLBACK}자 — 실측보다 부정확)")
    settings = load_settings()

    fixtures = sorted(Path(FIXTURES_DIR).glob("*.json"))
    targets = [args.tool] if args.tool else sorted(TOOLS)

    print("=" * 78)
    print(f"프롬프트 예산 측정 — {mode}")
    print(f"max_model_len {max_model_len} (출처: {src}) − 출력예약 {args.reserve} = "
          f"**프롬프트 예산 {budget} 토큰**")
    print(f"fixture {len(fixtures)}건 × tool {len(targets)}종")
    print("=" * 78)

    results: dict[str, list[tuple[str, int, dict[str, int]]]] = {t: [] for t in targets}
    empty_count = [0]   # 재료가 빈 fixture 수 — 많으면 측정이 과소라 신뢰할 수 없다
    for path in fixtures:
        try:
            alert = AlertModel.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue                                    # 손상 fixture(BROKEN)는 건너뛴다
        inputs = _tool_inputs(alert)
        empty_material = all(not v for v in inputs.values())
        if empty_material:
            empty_count[0] += 1
        for name in targets:
            try:
                sys_prompt, payload, example = TOOLS[name](alert, inputs.get(name, {}))
            except Exception as exc:  # noqa: BLE001
                log.warning("%s / %s payload 조립 실패: %s", name, path.name, exc)
                continue
            messages = build_messages(sys_prompt, payload, schema_example=example)
            total = _count(_messages_text(messages), tok)
            parts = {
                "system(§0+tool+예시)": _count(messages[0]["content"], tok),
                "payload(재료)": _count(messages[1]["content"], tok),
            }
            results[name].append((path.name, total, parts))

    exceeded_any = False
    unmeasured: list[str] = []          # 한 건도 못 잰 tool — 초록불로 넘기면 안 된다
    for name in targets:
        rows = results[name]
        if not rows:
            # 🔴 조립이 전부 실패한 tool 이다. 예전엔 여기서 조용히 continue 했는데,
            #    그러면 `exceeded_any` 가 False 로 남아 **측정을 안 하고 초록불**이 된다
            #    (2026-08-10 실측: release 합성 재료가 Pydantic 검증에 걸려 전건 실패했는데
            #     "✅ 전 fixture 예산 내" 가 찍혔다). 게이트가 침묵하면 없는 것과 같다 —
            #    #162 "조용한 상태 게이트 무노출 금지".
            unmeasured.append(name)
            print(f"\n🔴 {name} — 측정 0건 (payload 조립이 전건 실패). 위 WARNING 확인 필요")
            continue
        totals = sorted(r[1] for r in rows)
        worst = max(rows, key=lambda r: r[1])
        over = [r for r in rows if r[1] > budget]
        exceeded_any = exceeded_any or bool(over)
        mark = "🔴 초과" if over else "✅"
        print(f"\n{mark} {name}")
        print(f"    최소 {totals[0]:6,} · 중위 {totals[len(totals)//2]:6,} · "
              f"최대 {totals[-1]:6,}   (예산 {budget:,})")
        print(f"    최대 구성: {worst[0]}")
        for k, v in worst[2].items():
            print(f"      {k:24} {v:6,}")
        if over:
            print(f"    🔴 예산 초과 {len(over)}/{len(rows)}건 — 최대 초과폭 "
                  f"{totals[-1] - budget:+,} 토큰")
            if args.verbose:
                for fn, t, _ in sorted(over, key=lambda r: -r[1])[:10]:
                    print(f"      {t:6,}  {fn}")

    print("\n" + "=" * 78)
    if empty_count[0]:
        print(f"⚠️  재료가 빈 fixture {empty_count[0]}건 — Qdrant·Postgres 가 떠 있는지 확인할 것.")
        print("    재료가 비면 payload 가 짧아져 **초과를 놓친다**(측정 과소).")
    if unmeasured:
        print(f"🔴 측정 0건 tool {len(unmeasured)}종: {', '.join(unmeasured)} — "
              "**재지 못한 것은 통과가 아니다.** 조립 실패를 먼저 고칠 것.")
    if exceeded_any:
        print("🔴 예산 초과 있음 — 이 상태로 라운드를 돌리면 해당 tool 이 400 으로 fallback 된다.")
        print("   선택: ① max_model_len 상향(VRAM 여유 확인) ② 재료 축소 ③ 프롬프트 축약")
    if not exceeded_any and not unmeasured:
        print(f"✅ 전 fixture 예산 내 — 400 으로 죽지 않는다. (tool {len(targets)}종 전부 측정됨)")
    print("=" * 78)
    return 1 if (exceeded_any or unmeasured) else 0


if __name__ == "__main__":
    sys.exit(main())
