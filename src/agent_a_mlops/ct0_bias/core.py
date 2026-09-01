# -*- coding: utf-8 -*-
"""CT⓪ Model R2R 코어 — **순수 함수만** (Kafka·DB·파일을 모른다).

설계 정본: `docs/CT0_ModelR2R_설계방향_v1.md` §5·§6·§11.
산식은 멘토 확정(7/11)·`config/params.yaml` `ct.model_r2r` 가 정본이며 **여기서 바꾸지 않는다**:

    잔차   = actual_c65 − raw_pred            (raw_pred = predicted_c65 − bias_applied, D1)
    bias   = 최근 N(=500) 잔차의 rolling mean  (자격: pm_count ≥ 마지막 요란 PM 경계)
    적용   = |bias| ≤ 1.0 × train_rmse 로 clamp, 표본 < 50 이면 미적용(0.0)

왜 이 파일에 I/O 가 없나: 브로커·DB 없이 산식·경계·멱등을 전부 테스트하기 위해서다
(설계 §11). 부작용이 필요한 판단은 전부 `BiasDecision` 이라는 **값**으로 돌려주고,
기록·파일 교체·커밋 순서는 `updater` 가 책임진다 (설계 원칙 2 — 기록 없으면 갱신 없음).

누수 가드 (헌법 1-3): `actual_c65` 는 **bias 추정·채점·재학습 라벨 전용**이다. 이 모듈이
만드는 어떤 값도 피처 테이블로 흘러가면 안 된다 — 가드는 `assert` 가 아니라 `raise`
(헌법 7장: `python -O` 한 방에 방어선이 사라진다).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

# ── 상수 (헌법 6-1: 매직 넘버 금지) ────────────────────────────────────────
CT_TYPE = "ct0_bias"                 # ct_decisions.ct_type (db/init.sql 주석 정본)
CT_ID_SUFFIX = "R2R"                 # CT①의 `-XGB` · CT②의 `-AE` 와 대칭 (헌법 6-4)

MODE_OFF = "off"                     # updater 미가동 · 서빙 미적용 (기본값)
MODE_SHADOW = "shadow"               # 계산·기록만 (SHADOW 행) — 발행·적용 0
MODE_ACTIVE = "active"               # 실적용 (설계 §13 P2 — Scorecard 실증 후)
MODES = (MODE_OFF, MODE_SHADOW, MODE_ACTIVE)

# ct_decisions.retrain_status — 설계 §10 의 4종 + `DISABLED` (VARCHAR(16) 이내).
STATUS_APPLIED = "APPLIED"           # 서빙 bias 변경 (active)
STATUS_SHADOW = "SHADOW"             # 모의 — 값은 계산했으나 적용 안 함 (shadow)
STATUS_RESET = "RESET"               # 요란 판정 PM → bias=0 + 이전 라벨 차단
STATUS_CAP_EXCEEDED = "CAP_EXCEEDED"  # |raw bias| > clamp 상한 (헌법 1-1 ⓒ Incident 신호)
# `DISABLED` = 보정 해제(0 복귀). 설계 §10 목록에는 없었지만 **행 없는 서빙 변경**을 만들지
# 않으려고 추가했다 (설계 §15-2 ④) — 표본 미달·분모 결손으로 bias 를 0 으로 되돌리는 것도
# 엄연한 서빙값 변경이라, 기록 없이 바꾸면 "왜 보정이 꺼졌나"를 감사로 답할 수 없다.
STATUS_DISABLED = "DISABLED"
RECORDED_STATUSES = (STATUS_APPLIED, STATUS_SHADOW, STATUS_RESET, STATUS_CAP_EXCEEDED,
                     STATUS_DISABLED)

STATUS_INSUFFICIENT = "INSUFFICIENT"  # 표본 < min_labels (내부 구분용 — 기록은 DISABLED)

# 더미 폴백 예측의 `model_version` (consumer.dummy_prediction). 계약 §2 가 "실모델 예측과
# 구분해야 **채점·Model R2R 에서 오염되지 않음**"이라고 못박은 값이라 창에서 배제한다.
DUMMY_MODEL_VERSION = "dummy"

NO_BOUNDARY = -1                     # valid_from_pm_count 초기값 = "요란 리셋 이력 없음"
BIAS_ROUND = 2                       # 눈금 = C65 개수 (predicted_c65 관례, 설계 §5)
TRIGGER_REASON_MAX = 64              # ct_decisions.trigger_reason VARCHAR(64)
CT_ID_MAX = 64                       # ct_decisions.ct_id VARCHAR(64)
_SEQ_DIGITS = 6                      # 결정론 채번 SEQ 자릿수 (wafer_id 다이제스트)


# ── config ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BiasConfig:
    """`ct.model_r2r` 스냅샷. **로드는 호출자 책임** — 여기서는 dict 만 받는다(순수).

    코드 폴백 기본값이 `off` 인 이유: params 로드 실패 시 `{}` 가 되는데 그때 보정이
    켜지면 "설정을 못 읽었으니 무인 적용"이 된다 (`ct1_auto_promote` 와 같은 규율).
    """

    mode: str = MODE_OFF
    window_n: int = 500                  # C8 residual_window_n
    min_labels: int = 50                 # C8 min_labels_for_auto (SE≈10.7)
    max_rmse_ratio: float = 1.0          # C7 bias_max_rmse_ratio
    min_update_delta: float = 1.0        # D4 δ — 이만큼 움직여야 갱신·기록
    reset_on: str = "loud_pm_verdict"    # C8 reset_on (요란 판정 PM 에서만 리셋)

    @classmethod
    def from_params(cls, ct_params: dict | None) -> "BiasConfig":
        """`params.yaml` 의 `ct:` 절(dict)에서 `model_r2r` 를 읽는다. 결손 키는 기본값.

        값 오류(음수·0·미지의 mode)는 **예외가 아니라 안전 방향으로 정규화**한다 —
        설정 한 줄 때문에 예측 파이프라인이 죽으면 안 된다 (헌법 6-2). 대신 호출자가
        볼 수 있게 `validate()` 가 경고 목록을 돌려준다.
        """
        m = ((ct_params or {}).get("model_r2r") or {}) if isinstance(ct_params, dict) else {}
        if not isinstance(m, dict):
            m = {}
        mode = str(m.get("mode", MODE_OFF)).strip().lower()
        return cls(
            mode=mode if mode in MODES else MODE_OFF,
            window_n=_pos_int(m.get("residual_window_n"), cls.window_n),
            min_labels=_pos_int(m.get("min_labels_for_auto"), cls.min_labels),
            max_rmse_ratio=_pos_float(m.get("bias_max_rmse_ratio"), cls.max_rmse_ratio),
            min_update_delta=_nonneg_float(m.get("min_update_delta"), cls.min_update_delta),
            reset_on=str(m.get("reset_on", cls.reset_on)),
        )

    def validate(self, raw: dict | None = None) -> list[str]:
        """정규화 과정에서 삼킨 오설정을 문자열 목록으로 돌려준다 (기동 시 1회 경고용)."""
        warns: list[str] = []
        m = ((raw or {}).get("model_r2r") or {}) if isinstance(raw, dict) else {}
        if isinstance(m, dict) and str(m.get("mode", MODE_OFF)).strip().lower() not in MODES:
            warns.append(f"알 수 없는 mode={m.get('mode')!r} → {MODE_OFF} 로 잠금")
        if self.reset_on != "loud_pm_verdict":
            warns.append(f"reset_on={self.reset_on!r} 은 미지원 — 요란 판정 PM 리셋만 구현됨")
        if self.min_update_delta <= 0:
            warns.append("min_update_delta=0 — 라벨마다 기록이 남는다 (D4 양자화 무력화)")
        return warns


def _pos_int(v, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _pos_float(v, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) and f > 0 else default


def _nonneg_float(v, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) and f >= 0 else default


# ── 상태 (설계 §5) ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Label:
    """창 엔트리 1건 — 라벨 wafer 하나가 만든 **raw 예측 대비** 잔차."""

    wafer_id: str
    residual: float
    pm_count: int | None = None
    model_version: str | None = None


@dataclass
class BiasState:
    """챔버 1개의 보정 상태. 키가 `chamber_id` **단독**인 근거는 설계 D2(혼합 창).

    이 객체는 캐시다 — 진실 원천은 DB(`wafer_predictions` + `ct_decisions`)이고 언제든
    `bootstrap.rebuild_state` 로 재계산된다 (설계 원칙 3).
    """

    chamber_id: str
    valid_from_pm_count: int = NO_BOUNDARY
    window: deque = field(default_factory=deque)
    seen: set = field(default_factory=set)         # wafer_id 중복 편입 금지 (설계 §8)
    bias_applied: float = 0.0                      # 현재 서빙값 (clamp·양자화 후)
    train_rmse: float | None = None                # clamp 분모 — >0 검증 필수
    source_ct_id: str | None = None                # 마지막 갱신 감사 행 (추적성)
    last_status: str | None = None                 # 마지막 기록 상태 — **상태 전이도 기록 사유**

    @property
    def n(self) -> int:
        return len(self.window)


# ── 잔차·자격·창 (설계 §5·D1) ─────────────────────────────────────────────
def raw_prediction(predicted_c65, bias_applied) -> float | None:
    """발행값에서 **보정 전 예측**을 복원한다: `raw = predicted − bias_applied` (D1).

    이 복원이 되기 때문에 창을 개루프로 유지할 수 있다. 보정 후 예측 대비 잔차로
    갱신하면 60일 지연 하의 되먹임 루프가 된다 (설계 원칙 5).
    """
    p = _finite(predicted_c65)
    if p is None:
        return None
    b = _finite(bias_applied)
    return p - (b if b is not None else 0.0)


def residual_of(actual_c65, predicted_c65, bias_applied=0.0) -> float | None:
    """`actual − raw_pred`. 비유한·비수치는 `None` (호출자가 skip + log — 헌법 6-2)."""
    a = _finite(actual_c65)
    raw = raw_prediction(predicted_c65, bias_applied)
    if a is None or raw is None:
        return None
    r = a - raw
    return r if math.isfinite(r) else None


def is_eligible(label_pm_count, valid_from_pm_count: int) -> bool:
    """창 편입 자격 — **정수 비교**라 이벤트 순서 역전·tz 함정과 무관하다 (설계 §5).

    `pm_count` 결손(구 페이로드·수기 주입)은 **경계가 있을 때만** 배제한다. 경계가 없으면
    (= 요란 리셋 이력 없음) 어차피 전량 자격이라 배제할 이유가 없고, 경계가 생긴 뒤에는
    "어느 레짐인지 모르는 라벨"이므로 창을 오염시키지 않는 쪽(배제)이 안전하다.
    """
    if valid_from_pm_count <= NO_BOUNDARY:
        return True
    try:
        return int(label_pm_count) >= valid_from_pm_count
    except (TypeError, ValueError):
        return False


def is_dummy(model_version) -> bool:
    """더미 폴백 예측인가 (계약 §2 — `model_version="dummy"`).

    `consumer.dummy_prediction` 은 모델 로드·추론이 실패했을 때 **난수**(100±10)를 발행한다.
    파이프라인 생존을 위한 안전망이지만 예측이 아니므로, 60일 뒤 도착한 라벨과 짝지으면
    잔차가 수백 단위로 나와 rolling mean 을 통째로 끌어간다. 계약이 이 값을 남긴 이유가
    "채점·Model R2R 에서 오염되지 않게"라 여기서 배제한다.
    """
    return str(model_version or "").strip().lower() == DUMMY_MODEL_VERSION


def push_label(state: BiasState, label: Label, cfg: BiasConfig) -> bool:
    """창에 1건 편입. 반환 = 실제로 들어갔는지 (중복·부적격·비유한·더미면 False).

    같은 wafer_id 재전달을 막는 이유: Kafka 재전달·재기동 재처리가 같은 잔차를 여러 번
    넣으면 rolling mean 이 그 wafer 쪽으로 조용히 기운다 (설계 §8 '중복·재전달').
    """
    if label.wafer_id in state.seen:
        return False
    if not math.isfinite(label.residual):
        return False
    if is_dummy(label.model_version):
        return False
    if not is_eligible(label.pm_count, state.valid_from_pm_count):
        return False
    state.window.append(label)
    state.seen.add(label.wafer_id)
    _trim(state, cfg.window_n)
    return True


def _trim(state: BiasState, window_n: int) -> None:
    """창을 N 으로 자른다. `deque(maxlen=)` 을 쓰지 않는 이유: 밀려난 엔트리를 `seen`
    에서 같이 빼야 하는데 maxlen 은 조용히 버려서 `seen` 만 무한 성장한다."""
    while len(state.window) > max(1, window_n):
        old = state.window.popleft()
        state.seen.discard(old.wafer_id)


def apply_reset(state: BiasState, pm_count) -> bool:
    """요란 판정 PM 리셋 — 경계 상향 + **자격 필터 재적용** + bias=0. 반환 = 상태 변경 여부.

    설계 §4 는 "창 비움"이라 적었지만 구현은 **`pm_count ≥ 새 경계` 인 엔트리를 남긴다**
    (설계 §15-2 ③). 라벨이 60일 지연이라 실전에서는 리셋 시점의 창이 전부 구레짐이므로
    결과는 같고, 리셋이 늦게 도착하는 경로(부트스트랩 복구·유실 재전달)에서만 다르다 —
    그때 무조건 비우면 이미 유효한 신레짐 표본까지 날려 보정이 불필요하게 오래 꺼진다.

    **멱등**: 같은 이벤트가 다시 와도 `pm_count ≤ 현재 경계` 면 아무 것도 하지 않는다
    (False). 이 가드가 없으면 재전달 1건이 그동안 쌓인 신레짐 창을 통째로 날린다.
    """
    try:
        pc = int(pm_count)
    except (TypeError, ValueError):
        return False
    if pc <= state.valid_from_pm_count:
        return False
    state.valid_from_pm_count = pc
    kept = deque(e for e in state.window if is_eligible(e.pm_count, pc))
    state.window = kept
    state.seen = {e.wafer_id for e in kept}
    state.bias_applied = 0.0
    return True


# ── 결정 (설계 §6 D4·D5) ──────────────────────────────────────────────────
@dataclass(frozen=True)
class BiasDecision:
    """`decide()` 의 산출 — 부작용 없는 **판단 값**. 기록·적용 순서는 updater 책임."""

    status: str                      # 위 STATUS_* (INSUFFICIENT·DISABLED 포함)
    bias: float                      # 서빙해야 할 값 (clamp·반올림 후)
    raw_bias: float | None           # clamp 전 rolling mean (ct_decisions.residual)
    n: int                           # 창 표본 수
    cap: float | None                # clamp 상한 (= max_rmse_ratio × train_rmse)
    changed: bool                    # 서빙값이 바뀌는가 (= 기록·파일 교체 필요)
    cap_exceeded: bool = False
    reason: str = ""

    @property
    def record_status(self) -> str:
        """`ct_decisions.retrain_status` 에 실을 값 — 표본 미달은 `DISABLED` 로 합친다.

        내부 구분(`INSUFFICIENT`)은 로그·사유 문자열에만 남긴다. 감사 조회 쪽에서는
        "보정이 꺼졌다"가 한 값이어야 다루기 쉽다.
        """
        return STATUS_DISABLED if self.status == STATUS_INSUFFICIENT else self.status

    def should_record(self, last_status: str | None = None) -> bool:
        """행을 남길지 — '서빙값 변경 = 갱신 = 행 1개' (D4 실행 정의) **+ 상태 전이**.

        상태 전이를 더한 이유: `APPLIED(100)` 에서 raw 만 커져 `CAP_EXCEEDED(clamp 100)` 로
        넘어가면 **서빙값은 그대로**라 값 기준으로는 무기록이 된다. 그러면 CAP 에스컬레이션
        이벤트가 DB 에 없는 `ct_id` 를 가리키게 되고(dangling), "언제 상한을 넘기 시작했나"를
        감사로 답할 수 없다.
        """
        if self.record_status not in RECORDED_STATUSES:
            return False
        return self.changed or (last_status is not None and self.record_status != last_status)


def decide(state: BiasState, cfg: BiasConfig) -> BiasDecision:
    """창·설정으로부터 다음 서빙 bias 를 판단한다 (순수).

    순서가 곧 안전 규율이다:
      ⓐ mode=off / train_rmse 결손·≤0 → **미적용**. 0 분모는 매 건 예외 → 전량 폴백이라
        조용한 기능 정지가 된다 (헌법 7장). 여기서 명시적으로 끈다.
      ⓑ 표본 < min_labels → 미적용. **0 으로 가는 변경은 δ 양자화를 타지 않는다** —
        안전 방향(보정 해제)을 노이즈 필터가 지연시키면 안 된다.
      ⓒ clamp → 양자화. 상한 초과는 clamp 값으로 **서빙은 계속**하되 CAP_EXCEEDED 로
        기록·에스컬레이션한다 (헌법 1-1 ⓒ: 초과분은 적용하지 말고 Incident 신호로).
    """
    rmse = _finite(state.train_rmse)
    if cfg.mode == MODE_OFF or rmse is None or rmse <= 0:
        why = "mode=off" if cfg.mode == MODE_OFF else f"train_rmse 결손·비정상({state.train_rmse!r})"
        return _zero(state, STATUS_DISABLED, why)

    n = state.n
    if n < cfg.min_labels:
        return _zero(state, STATUS_INSUFFICIENT, f"표본 {n} < 최소 {cfg.min_labels}")

    raw = sum(e.residual for e in state.window) / n
    if not math.isfinite(raw):                       # 비유한 잔차는 push 에서 막히지만 2중 가드
        return _zero(state, STATUS_DISABLED, "rolling mean 비유한")

    cap = cfg.max_rmse_ratio * rmse
    clamped = max(-cap, min(cap, raw))
    exceeded = abs(raw) > cap
    bias = round(clamped, BIAS_ROUND)
    changed = abs(bias - state.bias_applied) >= cfg.min_update_delta

    if exceeded:
        status = STATUS_CAP_EXCEEDED
    else:
        status = STATUS_APPLIED if cfg.mode == MODE_ACTIVE else STATUS_SHADOW
    return BiasDecision(
        status=status, bias=bias, raw_bias=round(raw, 4), n=n, cap=round(cap, 4),
        changed=changed, cap_exceeded=exceeded,
        reason=(f"raw={raw:.2f} cap=±{cap:.2f} n={n}"
                + (" 상한초과→clamp" if exceeded else "")))


def _zero(state: BiasState, status: str, why: str) -> BiasDecision:
    """미적용(0.0) 결정. 이미 0 이면 changed=False (무행·무교체)."""
    return BiasDecision(status=status, bias=0.0, raw_bias=None, n=state.n, cap=None,
                        changed=state.bias_applied != 0.0, reason=why)


def dominant_model_version(state: BiasState, max_len: int = 32) -> str | None:
    """창에서 가장 많은 model_version (관찰용 — D2 혼합 창 감시).

    `ct_decisions.model_version_before` 에 싣는다. **`model_version_after` 는 비운다** —
    CT⓪ 은 모델을 바꾸지 않는다(출력 가산항만). 거기에 값을 넣으면 감사에서 CT①의
    모델 교체와 구분이 안 된다.
    """
    counts: dict[str, int] = {}
    for e in state.window:
        if e.model_version:
            counts[e.model_version] = counts.get(e.model_version, 0) + 1
    if not counts:
        return None
    top = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return top[:max_len]


def window_mix(state: BiasState) -> dict[str, int]:
    """창의 model_version 분포 (관측성 §10 — 로그·리포트용)."""
    mix: dict[str, int] = {}
    for e in state.window:
        k = e.model_version or "unknown"
        mix[k] = mix.get(k, 0) + 1
    return mix


def residual_stats(state: BiasState) -> tuple[float | None, float | None]:
    """창 잔차의 (mean, 표본표준편차) — 관측 지표. 표본 <2 면 σ=None."""
    n = state.n
    if n == 0:
        return None, None
    vals = [e.residual for e in state.window]
    mean = sum(vals) / n
    if n < 2:
        return mean, None
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    return mean, math.sqrt(var)


# ── ID 채번 (헌법 6-4 · 설계 §7 멱등) ─────────────────────────────────────
_TOKEN_RE = re.compile(r"[^A-Za-z0-9]")


def chamber_token(chamber_id: str | None) -> str:
    """`SIM_CH_3` → `SIMCH3` (업무 ID 포맷의 CHAMBER 자리 — 계약 §8 예시 관례)."""
    tok = _TOKEN_RE.sub("", str(chamber_id or "UNKNOWN")).upper()
    return tok or "UNKNOWN"


def ct_id_for(day: str, chamber_id: str | None, anchor: str) -> str:
    """`CT-<YYYYMMDD>-<CHAMBER>-<SEQ>-R2R` — **결정론 채번** (설계 §7 멱등).

    SEQ 를 순번이 아니라 `anchor`(트리거 라벨 wafer_id · 리셋 이벤트 qual_id)의
    다이제스트로 잡는 이유: 같은 메시지를 다시 처리하면 **같은 ct_id** 가 나와야
    `ON CONFLICT DO NOTHING` 이 중복 전달을 무해화한다. 순번 채번(CT①)은 DB 왕복이
    필요하고 재처리 때 새 번호를 뽑아 중복 행을 남긴다.

    충돌 위험: 같은 날·같은 챔버 안에서 10^6 분의 1. 그 경우 두 번째 갱신의 행이
    생략되는데(DO NOTHING), 서빙 갱신은 기록 성공을 전제로 하므로 **미적용으로 축퇴**한다
    (설계 원칙 6) — 조용한 오적용이 아니라 조용한 미적용이라 안전 방향이다.
    """
    seq = int(hashlib.sha1(f"{day}|{chamber_token(chamber_id)}|{anchor}".encode()).hexdigest()[:8],
              16) % (10 ** _SEQ_DIGITS)
    return f"CT-{day}-{chamber_token(chamber_id)}-{seq:0{_SEQ_DIGITS}d}-{CT_ID_SUFFIX}"[:CT_ID_MAX]


def reset_reason(pm_count: int, qual_id: str | None = None) -> str:
    """RESET 행의 `trigger_reason` — **부트스트랩 폴백이 여기서 경계를 되읽는다**.

    `quals.confirmed_verdict`(P6-1) 가 없는 동안 유일한 경계 복구 소스라 포맷을 고정한다
    (설계 §7 폴백). 파서는 `parse_reset_reason`.
    """
    base = f"loud_pm_reset pm_count={int(pm_count)}"
    if qual_id:
        base += f" qual={qual_id}"
    return base[:TRIGGER_REASON_MAX]


_QUAL_SEQ_RE = re.compile(r"-(\d+)\s*$")


def pm_count_from_qual_id(qual_id) -> int | None:
    """`QUAL-<YYYYMMDD>-<CHAMBER>-<SEQ>` 의 **SEQ = pm_count** (B `_judge_qual` 결정7).

    P6-1(`quals.confirmed_verdict`)이 배포되어 있으므로 요란 판정 PM 경계의 권위 소스는
    `quals` 다 (설계 §7 ①). 다만 `quals` 에 `pm_count` **컬럼**은 없고 `qual_id` 가 그 값을
    운반한다 — B 가 `f"QUAL-{date}-{chamber_id}-{pm_count}"` 로 채번하기 때문이다.

    ⚠️ 이건 **ID 포맷에 기대는 파싱**이라, 채번 규칙이 바뀌면 조용히 경계를 잃는다.
    그래서 ⓐ 판독 실패는 `None` 을 돌려 다른 소스(RESET 행·pm_log)로 넘기고 ⓑ 호출부가
    경고를 남긴다. 항구적 해소는 `quals.pm_count` 컬럼 신설이다 (설계 §15-3 ⑦ — P6-1 자체는
    이미 배포됐고, 남은 것은 이 파싱을 지우는 개선 하나뿐이다).
    """
    m = _QUAL_SEQ_RE.search(str(qual_id or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:                                # 정규식이 걸렀지만 방어적으로
        return None


_RESET_PM_RE = re.compile(r"pm_count=(\d+)")


def parse_reset_reason(reason: str | None) -> int | None:
    """RESET 행의 `trigger_reason` → pm_count. 판독 불가면 None (경계 없음으로 축퇴)."""
    m = _RESET_PM_RE.search(str(reason or ""))
    return int(m.group(1)) if m else None


def update_reason(dec: BiasDecision, mode: str) -> str:
    """갱신 행의 `trigger_reason` — `residual_bias` 접두 고정 (init.sql 주석 어휘)."""
    tail = f"n={dec.n}"
    if dec.raw_bias is not None:
        tail += f" raw={dec.raw_bias:.2f}"
    if dec.cap is not None:
        tail += f" cap={dec.cap:.1f}"
    return f"residual_bias mode={mode} {tail}"[:TRIGGER_REASON_MAX]


# ── 공용 유틸 ─────────────────────────────────────────────────────────────
def _finite(value) -> float | None:
    """유한 실수만 통과 (NaN/Inf/비수치 → None). 발행·DB 양쪽의 표준 JSON·NOT NULL 방어."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def labels_from_rows(rows: Iterable) -> list[Label]:
    """`(wafer_id, predicted_c65, bias_applied, actual_c65, pm_count, model_version)` 행 →
    `Label` 목록 (부트스트랩·테스트 공용, 순수).

    비유한·결손 행은 조용히 버린다 — 원천이 DB 든 Kafka 든 같은 규칙이어야 재계산
    determinism(설계 §10 리허설 ①)이 성립한다.
    """
    out: list[Label] = []
    for r in rows:
        wafer_id, predicted, bias_applied, actual, pm_count, model_version = (list(r) + [None] * 6)[:6]
        res = residual_of(actual, predicted, bias_applied)
        if wafer_id is None or res is None or is_dummy(model_version):
            continue
        try:
            pc = int(pm_count) if pm_count is not None else None
        except (TypeError, ValueError):
            pc = None
        out.append(Label(wafer_id=str(wafer_id), residual=res, pm_count=pc,
                         model_version=str(model_version) if model_version else None))
    return out
