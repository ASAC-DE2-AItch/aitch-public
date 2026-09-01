"""wafer 처분 판정 — SCRAP / RELEASE / HOLD (C6-1).

정본 = 기획서 v4.5 §4-3(의심 윈도우 모델) · config 합의안 B9 · 회의 브리핑 2026-07-06.

가드 테스트와 성격이 다르다: 여기서는 **판정의 옳고 그름을 코드가 따질 수 있다.**
처분은 수치 비교(predicted_c65 vs B9)라 정답이 결정적이기 때문 — 그래서 LLM 이 아니라
코드가 판정하고, 그래서 이 파일이 회귀를 실제로 막는다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_service.app.config import load_settings  # noqa: E402
from agent_service.app.disposition import (  # noqa: E402
    apply_disposition,
    decide_dispositions,
    decide_wafer,
    endpoints_only,
    heaviest,
    select_in_window,
)
from agent_service.app.schemas.alert import AlertModel  # noqa: E402
from agent_service.app.schemas.report import (  # noqa: E402
    WaferDisposition,
    WaferVerdict,
    make_fallback_brief,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alerts"
SETTINGS = load_settings()

# B9 = train P99 (config/params.yaml spc.crazy_wafer.predicted_c65_threshold)
B9 = float(SETTINGS.require("spc.crazy_wafer.predicted_c65_threshold"))

AGING = "02_baseline_aging_gate_edge.json"   # N3 추세 — suspect_window C64_995~C64_1001 (7장)
CRAZY = "44_edge_crazy_wafer.json"           # N1 단발 — 윈도우 없음, predicted 1650 > B9


def _alert(name: str) -> AlertModel:
    return AlertModel.model_validate(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


ANOM_MIN = 0.2   # 테스트용 anomaly 인정 하한 — 스케일 불가지(값 ≥ 하한이면 SCRAP)


def _settings_with_anomaly_min(minimum: float = ANOM_MIN):
    """`spc.crazy_wafer.anomaly_score_min` 이 설정된 Settings 사본.

    운영 params.yaml 에는 이 키가 **없다**(B9 재대조 미결) — 없으면 SCRAP 을 내지 않는 fail-safe 라서
    SCRAP 경로를 검증하려면 테스트가 임계를 명시해야 한다.
    """
    import copy
    import dataclasses

    params = copy.deepcopy(SETTINGS.params)
    params.setdefault("spc", {}).setdefault("crazy_wafer", {})["anomaly_score_min"] = minimum
    return dataclasses.replace(SETTINGS, params=params)


def _preds(**kv: float | None) -> dict[str, dict]:
    """{wafer_id: predicted} → API 응답 모양. anomaly 는 `_anom` 접미사로 따로 준다."""
    out: dict[str, dict] = {}
    for k, v in kv.items():
        wid, key = (k[:-5], "anomaly_score") if k.endswith("_anom") else (k, "predicted_c65")
        out.setdefault(wid, {})[key] = v
    return out


# --- 윈도우 해결 (§4-3 "HOLD 대상은 윈도우에서 자동 도출") ------------------------
# ⚠️ 실데이터(Data/문제1(하)/train_data.csv) 실측: wafer 번호 1~31,742 중 실재 11,939개
#    = **결손률 62.4%**. 번호를 연번으로 채우면 목록의 다수가 존재하지 않는 wafer 가 된다.
#    아래 테스트들이 그 회귀를 막는다 — "구간은 유효, 내부 생성은 금지".
def test_never_invents_missing_wafer_numbers() -> None:
    """구간 안을 지어내지 않는다 — 실재 결손 62.4% 라 연번 전개는 유령을 만든다."""
    wafers = endpoints_only(_alert(AGING))
    assert "C64_998" not in wafers                      # 있는지 모르는 번호는 만들지 않는다
    assert set(wafers) == {"C64_995", "C64_1001"}       # alert 이 실제로 준 id 만


def test_endpoints_only_without_suspect_window_is_single_wafer() -> None:
    """N1 단발은 §4-3 표에서 '해당 wafer 1장' — 폴백이 아니라 정답이다."""
    alert = _alert(CRAZY)
    assert alert.suspect_window is None
    assert endpoints_only(alert) == [alert.prediction_context.wafer_id]


def test_select_in_window_keeps_only_real_wafers() -> None:
    """정본 경로: 실재 목록에서 구간만 거른다 — 결손(996·997·999·1000)에 안전."""
    got = select_in_window(_alert(AGING), ["C64_993", "C64_995", "C64_998", "C64_1001", "C64_1005"])
    assert got == ["C64_995", "C64_998", "C64_1001"]    # 구간 밖 993·1005 제외, 결손은 애초에 없음


def test_member_wafers_beats_prediction_path() -> None:
    """alert 이 member_wafers 를 실어주면 A 예측 경로보다 우선한다 (B 요청 2026-07-28).

    B 목록은 **위반 판정에 실제로 쓴 레코드**라 예측 조회 실패분이 빠지지 않는다.
    A 경로는 예측이 있는 wafer 만 나오므로 중간 1장이 누락될 수 있다.
    """
    raw = json.loads((FIXTURES / AGING).read_text(encoding="utf-8"))
    raw["suspect_window"] = {"start_wafer": "C64_995", "end_wafer": "C64_1006",
                             "member_wafers": ["C64_1006", "C64_995", "C64_998"],  # 순서 섞음
                             "basis": "N3 추세 시작점 소급"}
    alert = AlertModel.model_validate(raw)
    # A 예측에는 998 이 없다 — member_wafers 를 쓰면 그래도 판정 대상에 들어간다
    preds = {"C64_995": {"predicted_c65": 700.0}, "C64_1006": {"predicted_c65": 710.0}}
    out = decide_dispositions(alert, preds, SETTINGS)
    assert [v.wafer_id for v in out.per_wafer] == ["C64_995", "C64_998", "C64_1006"]  # 번호순 정렬
    # 예측 없는 998 은 "조회 불가" → HOLD (창작 금지)
    assert {v.wafer_id: v.recommendation for v in out.per_wafer}["C64_998"] == "HOLD"


def test_member_wafers_absent_falls_back_unchanged() -> None:
    """member_wafers 가 없으면(현재 B 미구현) 기존 경로 그대로 — 선언만으로는 동작 변화 없음."""
    alert = _alert(AGING)
    assert alert.suspect_window is not None
    assert alert.suspect_window.member_wafers is None          # fixture 에 없음
    preds = {"C64_995": {"predicted_c65": 700.0}, "C64_998": {"predicted_c65": 710.0}}
    out = decide_dispositions(alert, preds, SETTINGS)
    assert [v.wafer_id for v in out.per_wafer] == ["C64_995", "C64_998"]  # A 예측 경로


def test_held_wafers_argument_still_wins() -> None:
    """인자 주입(mock·테스트)은 여전히 최우선 — member_wafers 보다 앞이다."""
    raw = json.loads((FIXTURES / AGING).read_text(encoding="utf-8"))
    raw["suspect_window"] = {"start_wafer": "C64_995", "end_wafer": "C64_1006",
                             "member_wafers": ["C64_995", "C64_998", "C64_1006"],
                             "basis": "테스트"}
    alert = AlertModel.model_validate(raw)
    out = decide_dispositions(alert, None, SETTINGS, held_wafers=["C64_999"])
    assert [v.wafer_id for v in out.per_wafer] == ["C64_999"]


def test_select_in_window_respects_multichamber_suffix() -> None:
    """계약 §1 멀티챔버 접미사(C64_516_CH_3) — 계열이 다르면 같은 번호라도 섞지 않는다."""
    raw = json.loads((FIXTURES / AGING).read_text(encoding="utf-8"))
    raw["suspect_window"] = {"start_wafer": "C64_10_CH_2", "end_wafer": "C64_20_CH_2",
                             "basis": "테스트"}
    alert = AlertModel.model_validate(raw)
    got = select_in_window(alert, ["C64_12_CH_2", "C64_15_CH_3", "C64_18_CH_2", "C64_30_CH_2"])
    assert got == ["C64_12_CH_2", "C64_18_CH_2"]


def test_predictions_are_the_wafer_list_source() -> None:
    """A 예측 응답이 곧 실재 목록 — 구간 안이어도 응답에 없으면 목록에 안 들어간다."""
    alert = _alert(AGING)
    preds = _preds(**{"C64_995": 700.0, "C64_998": 700.0, "C64_1001": 700.0})
    d = decide_dispositions(alert, preds, SETTINGS)
    assert d.held_wafers == ["C64_995", "C64_998", "C64_1001"]
    assert all(v.recommendation == "RELEASE" for v in d.per_wafer)


# --- wafer 1장 판정 트리 ---------------------------------------------------------
def test_release_when_prediction_under_threshold() -> None:
    v = decide_wafer("C64_1", B9 - 1, None, n_exceed=0, threshold=B9)
    assert v.recommendation == "RELEASE"
    assert f"{B9:g}" in v.reason


def test_hold_when_prediction_normal_but_anomaly_high() -> None:
    """STEP 2 anomaly 가드 (PM 결정 2026-07-29, 결정안 §8) — 예측 정상 + anomaly 높음 → RELEASE 금지.

    **왜**: anomaly 는 품질의 제2의 자가 아니라 **예측 유효성 게이트**다. 급변 wafer 는 예측이
    정상에 머문다(실측 — 부스트 20조합 전부 임계 미달. 트리 모델은 분포 밖을 외삽하지 않는다).
    즉 그 예측값 자체가 무의미한데, 가드가 없으면 "정상이니 풀어줘라"가 나온다.
    """
    v = decide_wafer("C64_1", B9 - 1, 0.83, n_exceed=0, threshold=B9, anomaly_min=ANOM_MIN)
    assert v.recommendation == "HOLD"
    assert "계측 대기" in v.reason
    assert v.predicted_c65 == B9 - 1 and v.anomaly_score == 0.83   # 두 자 모두 근거에 남는다


def test_release_when_both_rulers_normal() -> None:
    """대칭의 반대편 — 예측·anomaly 둘 다 정상이면 RELEASE (가드가 과잉 발동하지 않는다)."""
    v = decide_wafer("C64_1", B9 - 1, 0.01, n_exceed=0, threshold=B9, anomaly_min=ANOM_MIN)
    assert v.recommendation == "RELEASE"


def test_release_guard_inactive_while_threshold_unset() -> None:
    """임계 미등재 구간에는 가드가 발동하지 않는다 — **결함이 아니라 PM 이 정한 적용 순서**.

    값 0.5 는 확정됐으나 등재는 **AE 정상화 후**다(결정안 §8). AE 가 라이브에서 포화 중이라
    (예측 200건 전부 anomaly ≥ 0.5) 지금 등재하면 **RELEASE 가 전멸**해 라인이 선다.
    포화 상태에선 SCRAP 도 c65 축이 막아 유효하지 않으므로 미등재로 잃는 것이 없다.

    이 테스트는 그 중간 상태를 명시적으로 고정한다 — 등재 시 여기가 깨지는 것이
    **가드 활성 신호**이고, 그때 기대값을 HOLD 로 바꾸면 된다.
    """
    v = decide_wafer("C64_1", B9 - 1, 0.83, n_exceed=0, threshold=B9)  # anomaly_min 미지정
    assert v.recommendation == "RELEASE"


def test_scrap_only_with_spot_and_anomaly() -> None:
    """'한 장만 높게 나오면 스크랩' — 초과 1장 + anomaly 가 인정 임계 이상(이중 확인)."""
    v = decide_wafer("C64_1", B9 + 100, 0.83, n_exceed=1, threshold=B9, anomaly_min=ANOM_MIN)
    assert v.recommendation == "SCRAP"
    assert v.anomaly_score == 0.83


def test_no_scrap_when_anomaly_threshold_unset() -> None:
    """fail-safe(2026-07-28): 임계가 없으면 anomaly 가 와도 SCRAP 을 내지 않는다.

    구 구현은 값 유무(`anomaly is not None`)만 봐서, 필드가 붙는 순간 `0.01`(정상)도
    '동반 충족'으로 읽혀 SCRAP 이 과잉 발동했다. 임계값 확정(B9 재대조)까지 판정을 보류한다.
    """
    v = decide_wafer("C64_1", B9 + 100, 0.83, n_exceed=1, threshold=B9)  # anomaly_min 미지정
    assert v.recommendation == "HOLD"
    assert "임계 미확정" in v.reason        # 왜 보류했는지 근거에 남는다
    assert v.anomaly_score == 0.83          # 받은 값은 버리지 않는다


def test_no_scrap_when_anomaly_below_threshold() -> None:
    """임계 미달은 동반으로 인정하지 않는다 — 정상 수준 anomaly 로 버릴 수 없다."""
    v = decide_wafer("C64_1", B9 + 100, 0.01, n_exceed=1, threshold=B9, anomaly_min=ANOM_MIN)
    assert v.recommendation == "HOLD"
    assert "< 임계" in v.reason


def test_hold_when_anomaly_missing() -> None:
    """이중 확인 미충족 — 자 하나로는 되돌릴 수 없는 결정을 못 한다."""
    v = decide_wafer("C64_1", B9 + 100, None, n_exceed=1, threshold=B9, anomaly_min=ANOM_MIN)
    assert v.recommendation == "HOLD"
    assert "anomaly 미확인" in v.reason


def test_consecutive_exceed_is_never_scrap() -> None:
    """'여러 장이면 R2R' — 초과 2장 이상이면 wafer 문제가 아니라 공정 문제다."""
    v = decide_wafer("C64_1", B9 + 100, 0.83, n_exceed=2, threshold=B9, anomaly_min=ANOM_MIN)
    assert v.recommendation == "HOLD"
    assert "R2R" in v.reason


def test_missing_prediction_holds_without_inventing() -> None:
    """값이 없으면 창작하지 않고 원위치(HOLD) — limit 규칙6 과 같은 취지."""
    v = decide_wafer("C64_1", None, None, n_exceed=0, threshold=B9)
    assert v.recommendation == "HOLD"
    assert v.predicted_c65 is None


def test_boundary_is_release_not_scrap() -> None:
    """임계 '초과'가 조건이다 — 같은 값은 초과가 아니다(경계에서 버리지 않는다)."""
    assert decide_wafer("C64_1", B9, 3.6, 1, B9).recommendation == "RELEASE"


# --- 윈도우 전체 판정 ------------------------------------------------------------
def test_per_wafer_verdicts_can_differ_within_one_window() -> None:
    """§4-3 핵심: 25장 중 3장만 버린다. 윈도우 전체에 판정 하나를 주지 않는다."""
    alert = _alert(AGING)
    preds = _preds(**{f"C64_{n}": 700.0 for n in range(995, 1001)})
    preds["C64_1001"] = {"predicted_c65": B9 + 78, "anomaly_score": 0.83}

    d = decide_dispositions(alert, preds, _settings_with_anomaly_min())

    assert len(d.held_wafers) == 7
    assert len(d.per_wafer) == 7
    by_id = {v.wafer_id: v.recommendation for v in d.per_wafer}
    assert by_id["C64_1001"] == "SCRAP"
    assert all(by_id[f"C64_{n}"] == "RELEASE" for n in range(995, 1001))


def test_summary_takes_heaviest_verdict() -> None:
    """요약은 가장 되돌리기 어려운 판정 — 화면 한 줄이 위험을 낮춰 보이면 안 된다."""
    alert = _alert(AGING)
    preds = _preds(**{f"C64_{n}": 700.0 for n in range(995, 1002)})
    preds["C64_998"] = {"predicted_c65": B9 + 10, "anomaly_score": 0.91}

    d = decide_dispositions(alert, preds, _settings_with_anomaly_min())
    assert d.recommendation == "SCRAP"


def test_two_exceeding_wafers_downgrade_both_to_hold() -> None:
    """초과가 2장이면 둘 다 HOLD — 장수는 윈도우 폭이 아니라 **초과 장수**로 센다."""
    alert = _alert(AGING)
    preds = _preds(**{f"C64_{n}": 700.0 for n in range(995, 1000)})
    for w in ("C64_1000", "C64_1001"):
        preds[w] = {"predicted_c65": B9 + 50, "anomaly_score": 3.6}

    d = decide_dispositions(alert, preds, SETTINGS)
    assert d.recommendation == "HOLD"
    assert not any(v.recommendation == "SCRAP" for v in d.per_wafer)


def test_no_predictions_holds_every_wafer() -> None:
    """API_MODE=mock 등 조회가 아예 없는 상태 — 전부 HOLD 가 정직한 답이다."""
    d = decide_dispositions(_alert(AGING), None, SETTINGS)
    assert d.recommendation == "HOLD"
    assert all(v.recommendation == "HOLD" for v in d.per_wafer)
    assert all(v.predicted_c65 is None for v in d.per_wafer)


def test_held_wafers_never_empty() -> None:
    """비어 있으면 db wafer_dispositions(wafer_id NOT NULL) 적재가 불가능하다."""
    for name in (AGING, CRAZY):
        assert decide_dispositions(_alert(name), None, SETTINGS).held_wafers


def test_crazy_wafer_fixture_holds_without_anomaly() -> None:
    """44번 fixture 는 predicted 1650 > B9 이지만 anomaly 가 계약에 없어 HOLD 가 정답."""
    alert = _alert(CRAZY)
    pc = alert.prediction_context
    d = decide_dispositions(alert, {pc.wafer_id: {"predicted_c65": pc.predicted_c65}}, SETTINGS)
    assert d.recommendation == "HOLD"
    assert d.per_wafer[0].predicted_c65 == 1650.0


# --- Brief 병합 ------------------------------------------------------------------
def test_apply_disposition_overwrites_llm_verdict_but_keeps_reason() -> None:
    """수치 판정은 코드가 정본, 사람이 읽는 요약 한 줄은 LLM 것을 남긴다."""
    alert = _alert(AGING)
    brief = make_fallback_brief(alert, "INC-20260713-SIMCH2-0002")
    brief = brief.model_copy(update={"wafer_disposition": WaferDisposition(
        recommendation="SCRAP", reason="LLM 이 쓴 요약 한 줄")})

    out = apply_disposition(brief, alert, None, SETTINGS)

    assert out.wafer_disposition.recommendation == "HOLD"      # 코드가 교정
    assert out.wafer_disposition.reason == "LLM 이 쓴 요약 한 줄"  # 서술은 보존
    # 예측 조회가 없으므로 끝점만 — 구간 내부를 지어내지 않는다(결손 62.4%)
    assert [v.wafer_id for v in out.wafer_disposition.per_wafer] == ["C64_995", "C64_1001"]


def test_apply_disposition_fills_per_wafer_even_when_llm_omitted_it() -> None:
    """LLM 이 wafer_disposition 을 비워 보내도 코드가 채운다 — 배선이 LLM 출력에 의존하지 않는다."""
    alert = _alert(AGING)
    brief = make_fallback_brief(alert, "INC-20260713-SIMCH2-0002")
    brief = brief.model_copy(update={"wafer_disposition": WaferDisposition()})  # 전부 기본값
    out = apply_disposition(brief, alert, None, SETTINGS)
    wd = out.wafer_disposition
    assert wd.per_wafer, "per_wafer 는 코드가 채운다"
    assert wd.recommendation == "HOLD"                       # heaviest 파생
    assert wd.held_wafers                                    # 목록도 채워진다


def test_apply_disposition_summary_is_derived_not_llm() -> None:
    """요약 recommendation 은 per_wafer 에서 **파생**된다 — LLM 값을 쓰지 않는다.

    예측을 주입해 1장이 SCRAP 이 되게 하면, LLM 이 RELEASE 라고 했어도 요약은 SCRAP 이어야 한다.
    """
    alert = _alert(AGING)
    preds = _preds(**{"C64_995": 700.0, "C64_1001": B9 + 50, "C64_1001_anom": 0.83})
    brief = make_fallback_brief(alert, "INC-20260713-SIMCH2-0002")
    brief = brief.model_copy(update={"wafer_disposition": WaferDisposition(
        recommendation="RELEASE", reason="LLM 요약")})
    out = apply_disposition(brief, alert, preds, _settings_with_anomaly_min())
    wd = out.wafer_disposition
    assert wd.recommendation == "SCRAP"                      # 가장 무거운 판정으로 파생
    by = {v.wafer_id: v.recommendation for v in wd.per_wafer}
    assert by["C64_995"] == "RELEASE" and by["C64_1001"] == "SCRAP"   # 장별로 다르다
    assert wd.reason == "LLM 요약"                            # 서술만 보존


def test_apply_disposition_keeps_none_reason_when_llm_gave_none() -> None:
    """LLM reason 이 없으면 None 을 유지한다 — 코드가 사람 문장을 창작하지 않는다."""
    alert = _alert(AGING)
    brief = make_fallback_brief(alert, "INC-20260713-SIMCH2-0002")
    brief = brief.model_copy(update={"wafer_disposition": WaferDisposition(recommendation="HOLD")})
    out = apply_disposition(brief, alert, None, SETTINGS)
    assert out.wafer_disposition.reason is None


def test_anomaly_exactly_at_threshold_is_scrap() -> None:
    """임계 '이상'이 조건이다 — 같은 값은 충족(`>=`). 경계에서 판정이 흔들리지 않게 고정."""
    v = decide_wafer("C64_1", B9 + 100, ANOM_MIN, n_exceed=1, threshold=B9, anomaly_min=ANOM_MIN)
    assert v.recommendation == "SCRAP"


def test_select_in_window_with_empty_candidates() -> None:
    """후보 목록이 비면 빈 결과 — 그러면 decide_dispositions 가 endpoints_only 로 폴백한다."""
    assert select_in_window(_alert(AGING), []) == []


def test_heaviest_order_is_reversibility_not_cost() -> None:
    def v(r):
        return WaferVerdict(wafer_id="C64_1", recommendation=r, reason="r")

    assert heaviest([v("RELEASE"), v("HOLD")]) == "HOLD"
    assert heaviest([v("HOLD"), v("SCRAP"), v("RELEASE")]) == "SCRAP"
    assert heaviest([]) is None
