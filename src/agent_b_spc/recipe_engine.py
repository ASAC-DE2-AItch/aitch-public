"""B5-4 Recipe Tuning 엔진 — 방향 판정 + 보수 스텝 (스펙 v2.2).

'정량은 B, 근거는 C'(계약 §5)의 B 조각. C 파이프라인이 게이트(context_score>=31) 통과 alert에
대해 호출하는 **순수 함수**(D1) — HTTP 아님. `_stub_tuning_api` 자리를 대체한다.

축소 스코프(D11 · PM 승인 2026-07-30): 앵커 F11에서 손잡이 4개가 동시 이동하고 recipe 내
setpoint std=0.00이라 **축별 기울기가 원리적으로 식별 불가**. 검증된 것은 C4→가스 monitor의
부호·단조성뿐 → v1은 "어느 손잡이를 어느 쪽으로"만 산출하고 "얼마나"는 정책값(D12).

헌법: 1-1(적용·발행 없음·D6 초과는 제안 미생성) · 1-3(런타임 실측 C65 접근 0) ·
3-3(SHAP 순위 무개입·대표 치환 금지) · 6-1(계수 params·부재 시 명시 실패) · 7장(NaN/Inf 미발행).
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from src.common.config_util import _key, _section

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"
_B9_MARKERS = ("B9", "C65")          # crazy 마커 — 조정 손잡이 아닌 예측 결과값(base.primary_violation)


@dataclass(frozen=True)
class RecipeTuningConfig:
    """엔진 정책값 + 손잡이 맵. 부재 시 명시 실패(6-1)."""

    delta_max_pct: float             # agent.recipe_delta_max_pct (D6=3.0 — 명목 대비 누적 예산)
    step_pct: float                  # spc.recipe_step_pct (신규, 1.0 — 정책값·게인 아님, D12)
    knob_map: dict                   # recipe_knob_map.yaml (nominal_setpoints·axes·forbidden)

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "RecipeTuningConfig":
        """params.yaml + recipe_knob_map.yaml 로드 (경로는 params 키, 하드코딩 금지 6-1)."""
        path = Path(path)
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        spc = _section(cfg, "spc", path)
        agent = _section(cfg, "agent", path)
        km_rel = _key(spc, "recipe_knob_map_path", "spc", path)
        km_path = path.resolve().parents[1] / km_rel      # repo 루트 기준
        with open(km_path, encoding="utf-8") as f:
            knob_map = yaml.safe_load(f) or {}
        _validate_knob_map(knob_map)          # D15 기동 불변식 — 맵 오편집 시 RuntimeError
        return cls(
            delta_max_pct=float(_key(agent, "recipe_delta_max_pct", "agent", path)),
            step_pct=float(_key(spc, "recipe_step_pct", "spc", path)),
            knob_map=knob_map,
        )


@dataclass(frozen=True)
class TuningProposal:
    """튜닝안 (C 리포트가 소비 — 스텁 5필드 상위호환, D2). escalate면 수치 필드 전부 None."""

    parameter_id: str | None         # 'C4' | 'temp_target' | None(에스컬)
    knob_axis: str | None
    basis_sensor: str | None         # SHAP 지목 C코드 그대로 (대표 치환 금지, 3-3)
    value_current: float | None
    value_proposed: float | None
    delta_pct: float | None
    direction: str | None            # 'down' | 'up'
    sensitivity: dict                # 기울기 아님 — 방향 근거·불확실성(D2 재정의)
    escalate_reason: str | None
    applied_value: float | None = None   # 직전 적용값(있으면) — C ⓐ: 승인 화면 '현재값' 정확화.
                                         #   value_current(=명목·누적 기준)과 별개. 첫 스텝·escalate·temp=None


# ── 순수 헬퍼 ────────────────────────────────────────────────────────────
def _finite(x) -> bool:
    """숫자이며 유한(NaN/Inf 아님) — 7장 오염 차단."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _as_dict(v):
    """위반 원소를 dict로 강제 — C가 Pydantic `Violation` 객체를 model_dump 없이 넘겨도 강건.

    계약(§3-2)은 `list[dict]`이나, C 어댑터(X1)가 변환을 누락하면 `isinstance(dict)` 필터가
    전량 탈락시켜 **조용히 None**(튜닝 0건)이 된다 — 크래시 없는 무증상 실패라 seam에서 방어한다.
    dict는 그대로, `.model_dump()` 있으면 호출, 그 외는 원본(다음 필터가 걸러냄).
    """
    if isinstance(v, dict):
        return v
    dump = getattr(v, "model_dump", None)
    if callable(dump):
        try:
            d = dump()
            if isinstance(d, dict):
                return d
        except Exception:                       # noqa: BLE001 — 이상 객체는 원본 반환(필터가 처리)
            pass
    return v


def _vsensor(v: dict) -> str | None:
    """위반 dict의 센서 C코드 (계약 'sensor' / 내부 'sensor_id' 양쪽 수용)."""
    return v.get("sensor") or v.get("sensor_id")


def _axis_of(sensor: str, axes: dict) -> tuple[str | None, dict | None]:
    """센서가 속한 축(knobs∪monitors)을 찾는다. 첫 매칭 반환, 없으면 (None, None).

    forbidden은 basis 금지가 아니라 parameter_id(knob) 금지다(§1-3·데이터 사전). C17은
    forbidden이나 temp monitor라 basis로 허용 — C12/C31/C65는 어느 축 monitor도 아니라
    자연히 미매칭으로 걸러진다(축 라우팅이 forbidden-skip을 포섭).
    """
    for name, ax in axes.items():
        if sensor in ax.get("knobs", []) or sensor in ax.get("monitors", []):
            return name, ax
    return None, None


def to_tuning_dict(proposal: "TuningProposal | None") -> dict | None:
    """`TuningProposal` → C 파이프라인이 소비하는 `tuning` dict (C 어댑터 대행).

    `build_payload(tuning=...)`·가드가 dict를 요구하므로 C의 `asdict` 수고를 B가 제공한다.
    필드는 스텁 5필드 상위호환(스텁의 `_provisional` 도장은 실 API라 미탑재). None → None.
    """
    return None if proposal is None else asdict(proposal)


def _validate_knob_map(knob_map: dict) -> None:
    """D15 기동 불변식 — 맵 오편집 방어. 위반 시 RuntimeError(기동 차단).

    forbidden은 basis 필터가 아니라 여기서 집행한다(런타임 total function D7은 **입력** 계약이지
    설정 오류가 아니다). ① 모든 축 knobs ∩ forbidden == ∅ ② active 축 knobs·monitors 비지 않음
    ③ active 비-target knob이 nominal_setpoints 전 recipe에 존재.
    """
    forbidden = set(knob_map.get("forbidden", []))
    nominal = knob_map.get("nominal_setpoints", {})
    for name, ax in knob_map.get("axes", {}).items():
        knobs = ax.get("knobs", [])
        bad = forbidden.intersection(knobs)
        if bad:                                      # ①
            raise RuntimeError(
                f"recipe_knob_map: 축 '{name}' knobs가 forbidden과 교집합 {sorted(bad)} (D15①)")
        if ax.get("status") != "active":
            continue
        if not knobs or not ax.get("monitors"):      # ②
            raise RuntimeError(
                f"recipe_knob_map: active 축 '{name}' knobs/monitors 비어 있음 — 도달 불가 (D15②)")
        if ax.get("mode") == "target":               # ③ target 모드(temp_target)는 nominal 예외
            continue
        for knob in knobs:
            for recipe, nm in nominal.items():
                if knob not in nm:
                    raise RuntimeError(
                        f"recipe_knob_map: active knob '{knob}'(축 {name})이 "
                        f"nominal_setpoints[{recipe}]에 없음 (D15③)")


def _sensitivity(cfg: RecipeTuningConfig) -> dict:
    """C 리포트가 불확실성을 정직하게 서술할 재료 (D2 — 기울기 아님)."""
    return {
        "basis": "direction_only",
        "direction_evidence": "F11 동일일 2일 +97.2%/+85.2% (부호·단조 일관)",
        "gain_available": False,
        "gain_reason": "앵커에서 손잡이 4개 동시 이동 — 축별 기울기 식별 불가(B5-4 §1-2)",
        "step_policy_pct": cfg.step_pct,
        "cumulative_budget_pct": cfg.delta_max_pct,
    }


def _escalate(cfg: RecipeTuningConfig, reason: str, *, axis=None, basis=None) -> TuningProposal:
    """수치 없는 에스컬레이션 제안 (승인 큐에 안 실림 — §3-3)."""
    return TuningProposal(parameter_id=None, knob_axis=axis, basis_sensor=basis,
                          value_current=None, value_proposed=None, delta_pct=None,
                          direction=None, sensitivity=_sensitivity(cfg), escalate_reason=reason)


# ── 진입점 ───────────────────────────────────────────────────────────────
def compute_tuning(*, violations: list, shap_top3: list, chamber_id: str,
                   recipe_id: str | None, applied_setpoints: dict | None = None,
                   temp_tuned_this_incident: bool = False,
                   cfg: RecipeTuningConfig) -> TuningProposal | None:
    """SHAP + 위반 → 1스텝 튜닝안. total(무raise, D7). None = 튜닝 대상 자체 없음(위반 0 등).

    C가 `resolve_alert_context`로 얻은 recipe_id·step과 `applied_setpoints`(직전 적용분, D10)를
    주입한다. `temp_tuned_this_incident`(C ⓑ·A) = **이 Incident에서 이미 온도 튜닝했는가** —
    온도는 명목 앵커가 없어 누적 상한을 못 재므로, Incident당 1스텝만 자동 제안하고 반복이면
    에스컬(True)하되 **새 Incident면 리셋**(False)돼 다시 제안한다. escalate 제안은 수치 전부 None.
    """
    try:
        return _compute(violations, shap_top3, recipe_id, applied_setpoints,
                        temp_tuned_this_incident, cfg)
    except Exception:                        # noqa: BLE001 — C 프로세스 장애 전파 금지(D7·6-2)
        logger.exception("recipe_engine 예외 — 수치 없이 escalate")
        return _escalate(cfg, "엔진 내부 예외 — 튜닝 보류")


def _compute(violations, shap_top3, recipe_id, applied_setpoints, temp_tuned_this_incident, cfg):
    axes = cfg.knob_map.get("axes", {})
    # 0) 입력 위생: 원소를 dict로 강제(Violation 객체 seam 방어) 후 dict·B9 마커 아님만 실위반.
    #    비유한(NaN/Inf)은 여기서 버리지 않고(§3-2 step0과 §5 상충 — §5=escalate 채택) basis 값
    #    검사에서 escalate 처리(무raise ≠ 무오염, D7·7장).
    coerced = [_as_dict(v) for v in violations]
    reals = [v for v in coerced
             if isinstance(v, dict)
             and _vsensor(v) not in _B9_MARKERS and v.get("rule_id") != "B9"]
    if not reals:
        return None                          # 튜닝 대상 자체 없음
    viol_by_sensor: dict = {}
    for v in reals:
        viol_by_sensor.setdefault(_vsensor(v), []).append(v)

    # 1) 대상 센서 선정: shap 문자열 순서 그대로(재정렬 금지 §1-5). 첫 축 매칭에서 중단.
    basis, axis_name, axis = None, None, None
    for s in shap_top3:
        if not isinstance(s, str) or s in _B9_MARKERS:
            continue
        name, ax = _axis_of(s, axes)
        if name is not None:
            basis, axis_name, axis = s, name, ax
            break
    if basis is None:
        return _escalate(cfg, "원인 축에 유효 측정 짝 없음 — 자동 튜닝 불가, 사람 판단 필요")

    # escalate_only 축 → 맵 reason 인용 (D11)
    if axis.get("status") == "escalate_only":
        reason = axis.get("reason", "관측 불가 축")
        return _escalate(cfg, f"{axis_name} 축 튜닝 불가 — {reason}", axis=axis_name, basis=basis)

    # basis 위반 확보 (방향 산출의 근거)
    bvs = viol_by_sensor.get(basis)
    if not bvs:
        return _escalate(cfg, "SHAP 원인 센서에 위반 없음 — 방향 근거 부족", axis=axis_name, basis=basis)

    # 2) 급변 차단: 해당 축 센서 위반에 N1 또는 CRITICAL → escalate (① 장비 이상 후보)
    axis_sensors = set(axis.get("knobs", [])) | set(axis.get("monitors", []))
    for v in reals:
        if _vsensor(v) in axis_sensors and (
                v.get("rule_id") == "N1" or str(v.get("severity", "")).upper() == "CRITICAL"):
            return _escalate(cfg, "급변(N1/CRITICAL) — 손잡이로 덮지 않음", axis=axis_name, basis=basis)

    v = bvs[0]
    ucl, lcl, cur = (v.get("control_limit_upper"), v.get("control_limit_lower"), v.get("current_value"))
    if not (_finite(ucl) and _finite(lcl) and _finite(cur)):   # 비유한 → 수치 없이 escalate(§5·7장)
        return _escalate(cfg, "위반 값 비유한(NaN/Inf) — 수치 산출 불가", axis=axis_name, basis=basis)
    if ucl - lcl <= 0:                       # 3) 밴드 퇴화 (D14)
        return _escalate(cfg, "관리선 밴드 퇴화 — 방향 산출 불가", axis=axis_name, basis=basis)

    # temp_target(BL8) 분기 — 목표 지정형, 게인 불요(D8). incident 플래그로 반복 감지(A)
    if axis.get("mode") == "target":
        return _temp_target(cfg, v, cur, ucl, lcl, axis_name, basis, temp_tuned_this_incident)

    # 3) 방향 판정 (D14 — center 부재 대응)
    mid = (ucl + lcl) / 2.0
    if cur > ucl:
        dev_sign = 1
    elif cur < lcl:
        dev_sign = -1
    elif cur == mid:
        return _escalate(cfg, "current == 밴드 중점 — 방향 불명", axis=axis_name, basis=basis)
    else:
        dev_sign = 1 if cur > mid else -1    # 밴드 내 패턴 룰(N2·N3): 중점 대비
    direction_sign = int(axis.get("direction_sign", 1))
    direction = "down" if dev_sign * direction_sign > 0 else "up"

    # 4) 크기 = 정책 고정 (D12) · 5) 기준값 (D10)
    knob = axis["knobs"][0]
    nominal = cfg.knob_map.get("nominal_setpoints", {}).get(recipe_id, {}).get(knob) \
        if recipe_id is not None else None
    if nominal is None:
        return _escalate(cfg, f"recipe_id={recipe_id!r} · knob={knob} 명목값 조회 불가",
                         axis=axis_name, basis=basis)
    if abs(nominal) < 1e-12:
        return _escalate(cfg, "명목값 0 — 비율 산정 불가", axis=axis_name, basis=basis)
    applied = (applied_setpoints or {}).get(knob)
    base = nominal if applied is None else applied   # ⚠ `or` 금지 — applied=0.0 폴백 함정(D10)
    if not _finite(base):
        return _escalate(cfg, "applied_setpoints 비유한 — 튜닝 보류", axis=axis_name, basis=basis)

    # 6) 값 조립 (일반 knob)
    step_signed = (1.0 if direction == "up" else -1.0) * cfg.step_pct
    value_current = round(float(nominal), 4)             # 시뮬 오프셋 앵커 — applied 금지
    value_proposed = round(base * (1.0 + step_signed / 100.0), 4)
    delta_pct = round((value_proposed - nominal) / nominal * 100.0, 4)
    if not _finite(value_proposed) or not _finite(delta_pct):
        return _escalate(cfg, "산출 결과 비유한 — 튜닝 보류", axis=axis_name, basis=basis)
    if abs(delta_pct) > cfg.delta_max_pct:               # 누적 예산 소진 = F16 루프 정상 종료
        return _escalate(cfg, f"누적 상한(D6 {cfg.delta_max_pct}%) 소진(|Δ|={abs(delta_pct):.2f}%) — "
                              "원인 재검토", axis=axis_name, basis=basis)
    return TuningProposal(parameter_id=knob, knob_axis=axis_name, basis_sensor=basis,
                          value_current=value_current, value_proposed=value_proposed,
                          delta_pct=delta_pct, direction=direction,
                          sensitivity=_sensitivity(cfg), escalate_reason=None,
                          applied_value=applied)          # C ⓐ — 직전 적용값(없으면 None)


def _temp_target(cfg, v, cur, ucl, lcl, axis_name, basis, temp_tuned_this_incident):
    """temp_target(BL8) — 밴드 중심 방향으로 정책 스텝 이동, 절대값 쌍(게인 불요, D8).

    ⚠️ **온도 반복 정책 (C ⓑ·A)**: 온도는 데이터에 setpoint 컬럼이 없고(D3) C17은 측정값(step4
    정착 std~42)이라 **명목 고정 앵커가 없어 누적 상한을 못 잰다** → delta_pct 가 매번 cur 대비
    ±step_pct라 D6(3%) 상한에 원리적으로 안 걸린다(무한 드리프트). 그래서 **Incident당 1스텝만
    자동 제안하고, 같은 Incident에서 반복(temp_tuned_this_incident=True)이면 사람에게 넘긴다(에스컬)**.
    **새 Incident면 False라 다시 제안** — 챔버 평생 1회용이 아니라 사안 단위(헌법 1-4). 누적 드리프트는
    매 스텝 승인(HITL)이 게이트한다. (v2: 온도 setpoint 수집 시 명목 앵커로 자동 누적 상한 승격)

    ⚠️ **ⓒ 미확정**: value_proposed 는 C17(측정값)에 스텝을 곱한 값을 temp_target(설정값)으로 제안한다.
    C17≡temp_target(오프셋 0) 가정 — 오프셋이 있으면 방향이 뒤집힐 수 있어 **BL8 시뮬에서 확인 필요**
    (C few-shot도 동일 가정 — 다르면 양쪽 동반 수정). 승인(HITL)이 1차 방어.
    """
    if temp_tuned_this_incident:
        return _escalate(cfg, "온도축 — 이 Incident에서 이미 1스텝 튜닝(반복) — 명목 앵커 부재로 사람 판단으로 에스컬(A)",
                         axis=axis_name, basis=basis)
    mid = (ucl + lcl) / 2.0
    if cur > ucl:
        dev_sign = 1
    elif cur < lcl:
        dev_sign = -1
    elif cur == mid:
        return _escalate(cfg, "current == 밴드 중점 — 방향 불명", axis=axis_name, basis=basis)
    else:
        dev_sign = 1 if cur > mid else -1
    if abs(cur) < 1e-12:
        return _escalate(cfg, "temp 현재값 0 — 비율 산정 불가", axis=axis_name, basis=basis)
    step_signed = -dev_sign * cfg.step_pct               # 편차 반대(중심 방향)
    direction = "up" if step_signed > 0 else "down"
    value_current = round(float(cur), 4)
    value_proposed = round(cur * (1.0 + step_signed / 100.0), 4)
    delta_pct = round((value_proposed - cur) / cur * 100.0, 4)
    if not _finite(value_proposed) or not _finite(delta_pct) or abs(delta_pct) > cfg.delta_max_pct:
        return _escalate(cfg, "temp 스텝 상한 초과/비유한 — 튜닝 보류", axis=axis_name, basis=basis)
    return TuningProposal(parameter_id="temp_target", knob_axis=axis_name, basis_sensor=basis,
                          value_current=value_current, value_proposed=value_proposed,
                          delta_pct=delta_pct, direction=direction,
                          sensitivity=_sensitivity(cfg), escalate_reason=None)
