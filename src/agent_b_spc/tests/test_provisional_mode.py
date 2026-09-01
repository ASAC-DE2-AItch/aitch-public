"""provisional_mode 테스트 (B6-3-a — 가한계 진입 순수 상태머신).

전부 순수(DB·Kafka 없이): 부작용(writer·reload·tttm·persist)은 주입 mock.
스펙: specs/B6-3-a_가한계진입_스펙플랜.md · 공유모델 specs/B6-3_가한계_overview.md.
"""
from __future__ import annotations

import pytest

from src.agent_b_spc import provisional_mode as pm
from src.agent_b_spc.provisional_mode import Phase, ProvisionalConfig, ProvisionalMode


def _cfg(provisional_k=5.0, seasoning_quiet=10, seasoning_loud=1000, k_sigma=3.0,
         settle_window_wafers=200, settle_consecutive_m=2, settle_min_wafers=600,
         settle_block_min_fill=0.5, deadband_k_se=2.0):
    return ProvisionalConfig(provisional_k=provisional_k, seasoning_quiet=seasoning_quiet,
                             seasoning_loud=seasoning_loud, k_sigma=k_sigma,
                             settle_window_wafers=settle_window_wafers,
                             settle_consecutive_m=settle_consecutive_m,
                             settle_min_wafers=settle_min_wafers,
                             settle_block_min_fill=settle_block_min_fill,
                             deadband_k_se=deadband_k_se)


# ── Pass 1: config · Phase enum · 골격 ──────────────────────────────

def test_config_loads_from_params():
    """A7·provisional_k 로드 (limit_engine 절, B 소유)."""
    cfg = ProvisionalConfig.load()
    assert cfg.provisional_k == 5.0                  # B6-3 광폭 배수 (실측 대기)
    assert cfg.seasoning_quiet == 10                 # A7 조용
    assert cfg.seasoning_loud == 1000                # A7 v2 요란 = Phase 1 기간
    assert cfg.k_sigma == 3.0                        # σ_ref 역산용


def test_phase_enum_has_four_states():
    """3단계 + 신호/정상 = PHASE_0·PHASE_1·AWAITING_PHASE2·NORMAL."""
    assert {Phase.PHASE_0, Phase.PHASE_1, Phase.AWAITING_PHASE2, Phase.NORMAL}
    assert Phase.PHASE_0 != Phase.NORMAL             # 구분됨


def test_unknown_chamber_is_normal():
    """PM 전(미지 챔버) → NORMAL (억제 아님)."""
    p = ProvisionalMode(_cfg())
    assert p.get_phase("SIM_CH_1") == Phase.NORMAL


def test_get_phase_reflects_state():
    """상태 세팅 시 get_phase 반영 (골격 — 내부 상태 노출)."""
    p = ProvisionalMode(_cfg())
    p._state["SIM_CH_1"] = {"phase": Phase.PHASE_0}
    assert p.get_phase("SIM_CH_1") == Phase.PHASE_0


# ── Pass 2: on_pm(Phase 0·멱등) · on_qual_verdict(quiet→NORMAL) ──────

def _persisted():
    """persist 주입 mock — 호출된 (chamber, state 스냅샷) 기록."""
    calls = []
    return calls, lambda ch, st: calls.append((ch, dict(st)))


def test_on_pm_enters_phase_0():
    """pm_count↑ → Phase 0 억제 + 카운터/버퍼 초기화 + persist."""
    calls, persist = _persisted()
    p = ProvisionalMode(_cfg(), persist=persist)
    p.on_pm("SIM_CH_1", 5)
    assert p.get_phase("SIM_CH_1") == Phase.PHASE_0
    st = p._state["SIM_CH_1"]
    assert st["post_pm_count"] == 0 and st["last_pm_count"] == 5
    assert calls[-1][0] == "SIM_CH_1"                # persist 호출


def test_on_pm_idempotent_on_duplicate_or_past():
    """멱등(Review1) — 같은/과거 pm_count 재전달 → 무시(카운터 리셋 방지)."""
    p = ProvisionalMode(_cfg())
    p.on_pm("SIM_CH_1", 5)
    p._state["SIM_CH_1"]["post_pm_count"] = 300       # 진행 중이라 가정
    p.on_pm("SIM_CH_1", 5)                            # 중복(같은 pm_count)
    p.on_pm("SIM_CH_1", 4)                            # 과거
    assert p._state["SIM_CH_1"]["post_pm_count"] == 300   # 리셋 안 됨


def test_on_pm_new_cycle_resets():
    """새 PM(pm_count 증가) → 카운터 리셋 + Phase 0 재진입."""
    p = ProvisionalMode(_cfg())
    p.on_pm("SIM_CH_1", 5)
    p._state["SIM_CH_1"]["post_pm_count"] = 300
    p.on_pm("SIM_CH_1", 6)                            # 새 PM
    assert p._state["SIM_CH_1"]["post_pm_count"] == 0 and p.get_phase("SIM_CH_1") == Phase.PHASE_0


def test_on_qual_quiet_returns_to_normal():
    """조용 verdict → provisional 미진입, NORMAL 복귀 + 경계=10(정보)."""
    calls, persist = _persisted()
    p = ProvisionalMode(_cfg(), persist=persist)
    p.on_pm("SIM_CH_1", 5)
    p.on_qual_verdict("SIM_CH_1", "quiet")
    assert p.get_phase("SIM_CH_1") == Phase.NORMAL
    assert p._state["SIM_CH_1"]["boundary"] == 10    # A7 조용


def test_on_qual_verdict_phase_guard():
    """멱등·phase 가드(Review F) — PHASE_0 아니면 verdict 무시."""
    p = ProvisionalMode(_cfg())
    p.on_pm("SIM_CH_1", 5)
    p.on_qual_verdict("SIM_CH_1", "quiet")           # → NORMAL
    p.on_qual_verdict("SIM_CH_1", "loud")            # PHASE_0 아니라 무시
    assert p.get_phase("SIM_CH_1") == Phase.NORMAL   # loud 무시됨


def test_on_qual_verdict_unknown_chamber_noop():
    """PM 없이 verdict만 → phase 가드로 no-op (크래시 X)."""
    p = ProvisionalMode(_cfg())
    p.on_qual_verdict("SIM_CH_9", "loud")            # 상태 없음
    assert p.get_phase("SIM_CH_9") == Phase.NORMAL


# ── Pass 3: loud → 광폭 provisional (인코딩·seam·중심 자체산출) ──────

_GK_S = ("SIM_CH_1", "C6_0", 4, "settled", "C11")     # sigma 그룹
_GK_Q = ("SIM_CH_1", "C6_0", 5, "settled", "C31")     # quantile 그룹
_GK_OTHER = ("SIM_CH_2", "C6_0", 4, "settled", "C11")  # 다른 챔버


def _limits():
    return {
        _GK_S: {"center": 100.0, "sigma": 10.0, "ucl": 130.0, "lcl": 70.0,
                "method": "sigma", "limit_version": "v1"},
        _GK_Q: {"center": 0.0, "sigma": 1.0, "ucl": 3.0, "lcl": -3.0,
                "method": "quantile", "limit_version": "v1"},
        _GK_OTHER: {"center": 50.0, "sigma": 5.0, "ucl": 65.0, "lcl": 35.0,
                    "method": "sigma", "limit_version": "v1"},
    }


class _FakeWriter:
    def __init__(self):
        self.rows = None
        self.calls = 0

    def write_provisional(self, rows):
        self.rows = rows
        self.calls += 1


class _FakeTTTM:
    def __init__(self):
        self.excluded = None

    def set_excluded(self, s):
        self.excluded = set(s)


def _pmode(**over):
    reloads = []
    w, t = _FakeWriter(), _FakeTTTM()
    kw = dict(limits=_limits(), writer=w, tttm=t, reload_fn=lambda: reloads.append(1))
    kw.update(over)
    p = ProvisionalMode(_cfg(), **kw)
    return p, w, t, reloads


def _enter_loud(p, chamber="SIM_CH_1", pm=5):
    p.on_pm(chamber, pm)
    p.on_qual_verdict(chamber, "loud")


def test_loud_enters_phase_1_with_boundary():
    """요란 → Phase 1 + 경계=1000(A7 v2)."""
    p, w, t, _ = _pmode()
    _enter_loud(p)
    assert p.get_phase("SIM_CH_1") == Phase.PHASE_1
    assert p._state["SIM_CH_1"]["boundary"] == 1000


def test_loud_calls_seams_in_order():
    """광폭 write → reload → set_excluded(union) 호출."""
    p, w, t, reloads = _pmode()
    _enter_loud(p)
    assert w.calls == 1 and len(reloads) == 1
    assert t.excluded == {"SIM_CH_1"}                # union (첫 챔버)


def test_provisional_sigma_encoding():
    """★σ그룹: 광폭을 sigma 컬럼에 인코딩 (Nelson from_limit이 sigma로 판정)."""
    p, w, t, _ = _pmode()
    _enter_loud(p)
    row = next(r for r in w.rows if r["group_key"] == _GK_S)
    # σ_ref=10, provisional_k=5 → half=50 · sigma_prov=5/3·10
    assert row["method"] == "sigma"
    assert row["sigma"] == pytest.approx(5 / 3 * 10)          # provisional_k/3·σ_ref
    assert row["ucl"] == pytest.approx(150.0) and row["lcl"] == pytest.approx(50.0)  # center±half
    assert row["center"] == 100.0                            # 버퍼 없음 → old_center
    # Nelson from_limit(center, sigma_prov): 3σ = center + 3·sigma_prov = center+half
    assert row["center"] + 3 * row["sigma"] == pytest.approx(row["ucl"])


def test_provisional_quantile_encoding():
    """분위수그룹: 광폭을 ucl·lcl에 인코딩 (Nelson from_quantile) · sigma 승계."""
    p, w, t, _ = _pmode()
    _enter_loud(p)
    row = next(r for r in w.rows if r["group_key"] == _GK_Q)
    # σ_ref=(3−(−3))/(2·3)=1 · half=5·1=5
    assert row["method"] == "quantile"
    assert row["ucl"] == pytest.approx(5.0) and row["lcl"] == pytest.approx(-5.0)   # center±half
    assert row["sigma"] == 1.0                               # 참고통계 승계


def test_center_from_phase0_buffer():
    """중심 자체산출(Review G) — Phase 0 버퍼 n≥min_n이면 버퍼 평균."""
    p, w, t, _ = _pmode()
    p.on_pm("SIM_CH_1", 5)
    p._phase0_center["SIM_CH_1"][_GK_S] = [505.0, 5]         # sum=505, n=5 → mean=101
    p.on_qual_verdict("SIM_CH_1", "loud")
    row = next(r for r in w.rows if r["group_key"] == _GK_S)
    assert row["center"] == pytest.approx(101.0)             # 버퍼 평균 (old 100 아님)
    assert row["ucl"] == pytest.approx(151.0)                # 101+half(50)


def test_center_fallback_when_buffer_insufficient():
    """데이터 부족(n<min_n) → old_center fallback (크래시 X, Review2)."""
    p, w, t, _ = _pmode()
    p.on_pm("SIM_CH_1", 5)
    p._phase0_center["SIM_CH_1"][_GK_S] = [100.0, 1]         # n=1 < min_n=3
    p.on_qual_verdict("SIM_CH_1", "loud")
    row = next(r for r in w.rows if r["group_key"] == _GK_S)
    assert row["center"] == 100.0                            # old_center


def test_make_provisional_only_this_chamber():
    """_make_provisional은 해당 챔버 그룹만 (타 챔버 gk 제외)."""
    p, w, t, _ = _pmode()
    _enter_loud(p, chamber="SIM_CH_1")
    gks = {r["group_key"] for r in w.rows}
    assert _GK_OTHER not in gks and {_GK_S, _GK_Q} <= gks


def test_set_excluded_union_across_chambers():
    """2챔버 순차 요란 → set_excluded는 union (첫 챔버 제외 보존, Review B)."""
    p, w, t, _ = _pmode()
    _enter_loud(p, chamber="SIM_CH_1", pm=5)
    _enter_loud(p, chamber="SIM_CH_2", pm=5)
    assert t.excluded == {"SIM_CH_1", "SIM_CH_2"}            # 둘 다 (교체 시맨틱 대비)


# ── Pass 4: on_wafer (A7 카운터·wafer dedup·중심 축적·Phase 2 신호) ──

from collections import namedtuple

_Pt = namedtuple("_Pt", "group_key value")


def test_on_wafer_counts_post_pm():
    """wafer마다 post_pm_count +1 (Phase 0)."""
    p, w, t, _ = _pmode()
    p.on_pm("SIM_CH_1", 5)
    p.on_wafer("SIM_CH_1", "W1", [_Pt(_GK_S, 100.0)])
    p.on_wafer("SIM_CH_1", "W2", [_Pt(_GK_S, 100.0)])
    assert p._state["SIM_CH_1"]["post_pm_count"] == 2


def test_on_wafer_dedup_same_wafer_id():
    """같은 wafer_id 중복 유입 → +1 안 함 (재전달 방어)."""
    p, w, t, _ = _pmode()
    p.on_pm("SIM_CH_1", 5)
    p.on_wafer("SIM_CH_1", "W1", [_Pt(_GK_S, 100.0)])
    p.on_wafer("SIM_CH_1", "W1", [_Pt(_GK_S, 100.0)])       # 중복
    assert p._state["SIM_CH_1"]["post_pm_count"] == 1


def test_on_wafer_wafer_unit_not_per_point():
    """★한 wafer에 그룹 point 여럿 → +1만 (point마다 세면 과다카운트)."""
    p, w, t, _ = _pmode()
    p.on_pm("SIM_CH_1", 5)
    p.on_wafer("SIM_CH_1", "W1", [_Pt(_GK_S, 100.0), _Pt(_GK_Q, 0.0)])  # 2 point
    assert p._state["SIM_CH_1"]["post_pm_count"] == 1        # wafer 단위 1


def test_on_wafer_phase0_accumulates_center():
    """Phase 0 wafer → 새-레짐 중심 버퍼 그룹별 축적 (new_center 소스)."""
    p, w, t, _ = _pmode()
    p.on_pm("SIM_CH_1", 5)
    p.on_wafer("SIM_CH_1", "W1", [_Pt(_GK_S, 100.0)])
    p.on_wafer("SIM_CH_1", "W2", [_Pt(_GK_S, 102.0)])
    buf = p._phase0_center["SIM_CH_1"][_GK_S]                # [sum, n]
    assert buf == [202.0, 2]                                 # 그룹별 러닝


def test_phase2_signal_at_boundary():
    """Phase 1에서 post_pm_count가 경계 도달 → AWAITING_PHASE2 + 신호."""
    p, w, t, _ = _pmode()
    p2 = ProvisionalMode(_cfg(seasoning_loud=3), limits=_limits(),
                         writer=w, tttm=t, reload_fn=lambda: None)
    p2.on_pm("SIM_CH_1", 5)
    p2.on_qual_verdict("SIM_CH_1", "loud")                   # Phase 1, 경계=3
    assert p2.on_wafer("SIM_CH_1", "W1", []) is None
    assert p2.on_wafer("SIM_CH_1", "W2", []) is None
    sig = p2.on_wafer("SIM_CH_1", "W3", [])                  # 3장 → 경계
    assert sig is not None and sig.chamber == "SIM_CH_1"
    assert p2.get_phase("SIM_CH_1") == Phase.AWAITING_PHASE2


def test_phase2_signal_only_once():
    """★신호 1회만 (Review3) — 경계 넘겨도 재발행 X."""
    w2, t2 = _FakeWriter(), _FakeTTTM()
    p = ProvisionalMode(_cfg(seasoning_loud=2), limits=_limits(),
                        writer=w2, tttm=t2, reload_fn=lambda: None)
    p.on_pm("SIM_CH_1", 5)
    p.on_qual_verdict("SIM_CH_1", "loud")
    p.on_wafer("SIM_CH_1", "W1", [])
    assert p.on_wafer("SIM_CH_1", "W2", []) is not None      # 경계 도달 신호
    assert p.on_wafer("SIM_CH_1", "W3", []) is None          # 그 후 재발행 X


def test_on_wafer_normal_chamber_noop():
    """NORMAL(PM 없음·조용 복귀) → 카운트·버퍼·신호 없음."""
    p, w, t, _ = _pmode()
    assert p.on_wafer("SIM_CH_9", "W1", [_Pt(_GK_S, 100.0)]) is None
    assert "SIM_CH_9" not in p._state


# ── Pass 5: restore(라운드트립·재제외) + 통합 라이프사이클 ────────────

def _state(phase, post=0, boundary=None, last_pm=5, verdict=None):
    return {"phase": phase, "post_pm_count": post, "boundary": boundary,
            "last_pm_count": last_pm, "verdict": verdict}


def test_restore_round_trips_state():
    """재시작 복구(Review E) — 전 필드 라운드트립."""
    p, w, t, _ = _pmode()
    p.restore({"SIM_CH_1": _state(Phase.PHASE_1, post=500, boundary=1000, verdict="loud")})
    st = p._state["SIM_CH_1"]
    assert st["phase"] == Phase.PHASE_1 and st["post_pm_count"] == 500
    assert st["boundary"] == 1000 and st["last_pm_count"] == 5 and st["verdict"] == "loud"
    assert p.get_phase("SIM_CH_1") == Phase.PHASE_1


def test_restore_reapplies_exclusion_union():
    """PHASE_1 챔버 → set_excluded(union) 재적용 (excluded는 tttm 인메모리라 유실)."""
    p, w, t, _ = _pmode()
    p.restore({"SIM_CH_1": _state(Phase.PHASE_1, boundary=1000),
               "SIM_CH_2": _state(Phase.PHASE_1, boundary=1000),
               "SIM_CH_3": _state(Phase.NORMAL)})
    assert t.excluded == {"SIM_CH_1", "SIM_CH_2"}            # PHASE_1만, union


def test_restore_resets_last_wafer_id():
    """last_wafer_id는 미영속(인메모리, ±1 허용) → 복구 시 None."""
    p, w, t, _ = _pmode()
    p.restore({"SIM_CH_1": _state(Phase.PHASE_1, post=500, boundary=1000)})
    assert p._state["SIM_CH_1"]["last_wafer_id"] is None


def test_restore_accepts_phase_value_string():
    """persist가 enum value(str)로 저장했어도 복구 (라운드트립 호환)."""
    p, w, t, _ = _pmode()
    p.restore({"SIM_CH_1": _state("phase_1", post=500, boundary=1000)})
    assert p.get_phase("SIM_CH_1") == Phase.PHASE_1


def test_restore_then_continues_counting():
    """복구 후 Phase 1 카운트 이어서 → 경계 도달 신호."""
    w2, t2 = _FakeWriter(), _FakeTTTM()
    p = ProvisionalMode(_cfg(seasoning_loud=502), limits=_limits(),
                        writer=w2, tttm=t2, reload_fn=lambda: None)
    p.restore({"SIM_CH_1": _state(Phase.PHASE_1, post=500, boundary=502)})
    assert p.on_wafer("SIM_CH_1", "W1", []) is None          # 501
    assert p.on_wafer("SIM_CH_1", "W2", []) is not None      # 502 → 신호


# ── Step 4-add: on_firm_established (firm 탈출 훅) + restore AWAITING 포함 ──

def _to_awaiting(p, ch, pm=5):
    """챔버를 AWAITING_PHASE2까지 몰기 (seasoning_loud=1 cfg 전제)."""
    p.on_pm(ch, pm)
    p.on_qual_verdict(ch, "loud")           # Phase 1 · _provisional 등록
    p.on_wafer(ch, "W1", [])                # 경계=1 → AWAITING_PHASE2


def _awaiting_pmode():
    w, t, reloads = _FakeWriter(), _FakeTTTM(), []
    p = ProvisionalMode(_cfg(seasoning_loud=1), limits=_limits(),
                        writer=w, tttm=t, reload_fn=lambda: reloads.append(1))
    return p, w, t


def test_on_firm_established_exits_to_normal():
    """AWAITING_PHASE2 → firm 확립 → NORMAL·provisional 해제·TTTM 복귀·버퍼 정리."""
    p, w, t = _awaiting_pmode()
    _to_awaiting(p, "SIM_CH_1")
    assert p.get_phase("SIM_CH_1") == Phase.AWAITING_PHASE2
    assert "SIM_CH_1" in p._provisional and "SIM_CH_1" in p._phase0_center
    p.on_firm_established("SIM_CH_1")
    assert p.get_phase("SIM_CH_1") == Phase.NORMAL          # firm 복귀
    assert "SIM_CH_1" not in p._provisional                 # union에서 제거
    assert t.excluded == set()                              # TTTM 복귀(남은 union 빔)
    assert "SIM_CH_1" not in p._phase0_center               # 버퍼 정리


def test_on_firm_established_guard_non_awaiting():
    """가드 — 非AWAITING_PHASE2(PHASE_0/1·NORMAL) 호출 → no-op(멱등, 반려 시 holding 유지)."""
    p, w, t = _awaiting_pmode()
    p.on_pm("SIM_CH_1", 5)                                  # PHASE_0
    p.on_firm_established("SIM_CH_1")
    assert p.get_phase("SIM_CH_1") == Phase.PHASE_0         # 안 바뀜
    p.on_qual_verdict("SIM_CH_1", "loud")                   # PHASE_1
    p.on_firm_established("SIM_CH_1")
    assert p.get_phase("SIM_CH_1") == Phase.PHASE_1         # 안 바뀜(정착 전)


def test_on_firm_established_one_of_two_keeps_union():
    """★2챔버 중 1개만 firm → 나머지 provisional union 유지 (Review B)."""
    p, w, t = _awaiting_pmode()
    _to_awaiting(p, "SIM_CH_1")
    _to_awaiting(p, "SIM_CH_2")
    assert t.excluded == {"SIM_CH_1", "SIM_CH_2"}
    p.on_firm_established("SIM_CH_1")                       # CH1만 firm
    assert p._provisional == {"SIM_CH_2"}                  # CH2 유지
    assert t.excluded == {"SIM_CH_2"}                      # 줄어든 union 전달


def test_on_firm_established_idempotent():
    """멱등 — 이미 NORMAL(firm 완료)에서 재호출 → no-op."""
    p, w, t = _awaiting_pmode()
    _to_awaiting(p, "SIM_CH_1")
    p.on_firm_established("SIM_CH_1")
    p.on_firm_established("SIM_CH_1")                       # 재호출
    assert p.get_phase("SIM_CH_1") == Phase.NORMAL and p._provisional == set()


def test_restore_reapplies_exclusion_for_awaiting_too():
    """🐛 수정 — restore가 PHASE_1뿐 아니라 AWAITING_PHASE2도 재-제외(둘 다 firm 전 provisional-active)."""
    p, w, t, _ = _pmode()
    p.restore({"SIM_CH_1": _state(Phase.PHASE_1, boundary=1000),
               "SIM_CH_2": _state(Phase.AWAITING_PHASE2, post=1000, boundary=1000)})
    assert p._provisional == {"SIM_CH_1", "SIM_CH_2"}      # 둘 다 (AWAITING 누락 방지)
    assert t.excluded == {"SIM_CH_1", "SIM_CH_2"}          # set_excluded 반영


def test_full_lifecycle_pm_to_phase2_signal():
    """관통 — PM→Phase0→loud→Phase1→정착→Phase2 신호."""
    w2, t2, reloads = _FakeWriter(), _FakeTTTM(), []
    p = ProvisionalMode(_cfg(seasoning_loud=3), limits=_limits(),
                        writer=w2, tttm=t2, reload_fn=lambda: reloads.append(1))
    p.on_pm("SIM_CH_1", 5)
    assert p.get_phase("SIM_CH_1") == Phase.PHASE_0          # 억제
    p.on_wafer("SIM_CH_1", "Q1", [_Pt(_GK_S, 101.0)])        # Phase0 (post=1)
    p.on_qual_verdict("SIM_CH_1", "loud")                    # → Phase1 (광폭 write·excluded)
    assert p.get_phase("SIM_CH_1") == Phase.PHASE_1
    assert w2.calls == 1 and t2.excluded == {"SIM_CH_1"} and len(reloads) == 1
    p.on_wafer("SIM_CH_1", "W1", [])                         # post=2
    sig = p.on_wafer("SIM_CH_1", "W2", [])                   # post=3 → 경계
    assert isinstance(sig, pm.Phase2Signal) and sig.chamber == "SIM_CH_1"
    assert p.get_phase("SIM_CH_1") == Phase.AWAITING_PHASE2


# ── B6-3-e: Phase 1 정착 σ 수렴 판정 ─────────────────────────────────
def _smode(whitelist=frozenset({_GK_S}), limits=None, **cfg_over):
    """정착 판정용 ProvisionalMode — 작은 W, whitelist 주입. _enter_loud로 PHASE_1 진입."""
    p = ProvisionalMode(_cfg(**cfg_over), limits=limits or _limits(),
                        writer=_FakeWriter(), tttm=_FakeTTTM(),
                        reload_fn=lambda: None, whitelist=set(whitelist))
    return p


def _feed(p, n, value=100.0, gk=_GK_S, ch="SIM_CH_1", start=0):
    """n장 유입 — 첫 신호 반환(신호 후 phase 전이라 이후 None)."""
    for i in range(start, start + n):
        v = value(i) if callable(value) else value
        pts = [_Pt(gk, v)] if gk is not None else []
        sig = p.on_wafer(ch, f"W{i}", pts)
        if sig is not None:
            return sig
    return None


def test_settle_converged_early():
    """상수 유입 → M연속 수렴 → 조기 정착(converged=True·settled_at=floor 12)."""
    p = _smode(settle_window_wafers=4, settle_consecutive_m=2, settle_min_wafers=0)
    _enter_loud(p)                                  # PHASE_1 + firm σ_ref 스냅샷
    sig = _feed(p, 20, value=100.0)                 # 블록 4·8·12 → streak 0·1·2
    assert sig is not None and sig.converged is True and sig.settled_at == 12
    assert p.get_phase("SIM_CH_1") == Phase.AWAITING_PHASE2


def test_settle_fallback_uncoverged():
    """블록마다 큰 이동(Δ≥band) → streak 리셋 → 미수렴 → 상한 폴백(converged=False·settled_at=boundary)."""
    p = _smode(settle_window_wafers=4, settle_consecutive_m=2, settle_min_wafers=0,
               seasoning_loud=12)                   # boundary=12
    _enter_loud(p)
    sig = _feed(p, 12, value=lambda i: 100.0 if (i // 4) % 2 == 0 else 500.0)  # 블록 교대
    assert sig is not None and sig.converged is False and sig.settled_at == 12


def test_settle_min_fill_defer():
    """블록 유효표본 n<W×min_fill → 유예(eval_ok False)·미정착."""
    p = _smode(settle_window_wafers=4, settle_consecutive_m=2, settle_min_wafers=0,
               settle_block_min_fill=0.5)           # min_n=2
    _enter_loud(p)
    p.on_wafer("SIM_CH_1", "W0", [_Pt(_GK_S, 100.0)])   # 1점만
    for i in range(1, 4):
        p.on_wafer("SIM_CH_1", f"W{i}", [])          # 나머지 결측 → 블록 n=1<2
    assert p._settle_eval_ok["SIM_CH_1"].get(_GK_S) is False


def test_settle_sigma_ref_degenerate_defer():
    """firm σ_ref≤EPS(퇴화) → 유예(0 나눗셈 회피)·미정착."""
    degen = {_GK_S: {"center": 100.0, "sigma": 0.0, "ucl": 100.0, "lcl": 100.0,
                     "method": "sigma", "limit_version": "v1"}}
    p = _smode(settle_window_wafers=4, settle_min_wafers=0, limits=degen)
    _enter_loud(p)
    _feed(p, 12, value=100.0)
    assert p._settle_eval_ok["SIM_CH_1"].get(_GK_S) is False
    assert p.get_phase("SIM_CH_1") == Phase.PHASE_1   # 미정착(폴백 전)


def test_settle_floor_gates_early():
    """streak≥M이어도 post_pm<floor면 정착 안 함(하한 가드)."""
    p = _smode(settle_window_wafers=4, settle_consecutive_m=2, settle_min_wafers=100)  # floor=100
    _enter_loud(p)
    sig = _feed(p, 20, value=100.0)                  # streak 2 도달(post 12)이나 floor 100 미달
    assert sig is None and p.get_phase("SIM_CH_1") == Phase.PHASE_1


def test_settle_defer_after_streak_blocks_settle():
    """streak≥M 후 유예 블록 → eval_ok False → 동시충족 배제(#4, 조기 정착 방지)."""
    p = _smode(settle_window_wafers=4, settle_consecutive_m=2, settle_min_wafers=100)  # floor 높여 조기 정착 차단
    _enter_loud(p)
    _feed(p, 12, value=100.0)                        # streak 2·eval_ok True
    assert p._settle_streak["SIM_CH_1"][_GK_S] >= 2
    for i in range(12, 16):
        p.on_wafer("SIM_CH_1", f"W{i}", [])          # 결측 블록 → 유예
    assert p._settle_eval_ok["SIM_CH_1"][_GK_S] is False
    assert p._is_settled("SIM_CH_1") is False         # streak 유지돼도 eval_ok False라 미정착


def test_settle_whitelist_excludes_nontarget():
    """whitelist 밖 그룹(_GK_Q)은 판정 대상 아님 — 그 값이 정착을 막지 않음."""
    p = _smode(whitelist={_GK_S}, settle_window_wafers=4, settle_consecutive_m=2, settle_min_wafers=0)
    _enter_loud(p)
    # _GK_S는 상수 수렴 / _GK_Q는 매 wafer 튀는 값(대상 아니라 무영향)
    for i in range(20):
        sig = p.on_wafer("SIM_CH_1", f"W{i}", [_Pt(_GK_S, 100.0), _Pt(_GK_Q, float(i * 99))])
        if sig is not None:
            break
    assert sig is not None and sig.converged is True   # _GK_Q 튐 무관 정착


def test_restore_rederives_settle_sigma_ref():
    """재시작 restore — 주입 firm σ_ref 재적재(광폭 아님·리뷰3#2)."""
    p = _smode()
    st = {"SIM_CH_1": {"phase": Phase.PHASE_1, "post_pm_count": 300,
                       "verdict": "loud", "boundary": 1000, "last_pm_count": 5}}
    p.restore(st, settle_sigma_ref={"SIM_CH_1": {_GK_S: 12.5}})
    assert p._settle_sigma_ref["SIM_CH_1"][_GK_S] == 12.5
    assert p.get_phase("SIM_CH_1") == Phase.PHASE_1


def test_phase2signal_new_fields_default():
    """Phase2Signal 필드 추가 무회귀 — settled_at·converged 기본값."""
    s = pm.Phase2Signal("SIM_CH_1")
    assert s.settled_at == 0 and s.converged is False


# ── B6-3-f: 요란 확정 → pm_log 기입 seam (순수 — 파일 I/O 없음) ──────

def _logger_mock():
    """pm_logger 주입 mock — 호출 payload 기록. `boom=True`면 예외(격리 검증)."""
    calls = []

    def _log(payload):
        calls.append(payload)
        if getattr(_log, "boom", False):
            raise RuntimeError("디스크 실패 시뮬")

    return _log, calls


def test_loud_calls_pm_logger_once_with_payload():
    """★요란 확정 → pm_logger 1회 + payload(chamber·pm_count·개방시각·verdict)."""
    log, calls = _logger_mock()
    p, _, _, _ = _pmode(pm_logger=log)
    p.on_pm("SIM_CH_1", 7, "2026-07-28T05:12:33.000+00:00")
    p.on_qual_verdict("SIM_CH_1", "loud")
    assert len(calls) == 1
    assert calls[0] == {"chamber_id": "SIM_CH_1", "pm_count": 7,
                        "pm_detected_at": "2026-07-28T05:12:33.000+00:00", "verdict": "loud"}


def test_quiet_does_not_call_pm_logger():
    """조용 판정은 레짐 전환이 아니다 — 기입 없음(규약: 요란 PM만)."""
    log, calls = _logger_mock()
    p, _, _, _ = _pmode(pm_logger=log)
    p.on_pm("SIM_CH_1", 7, "2026-07-28T05:12:33+00:00")
    p.on_qual_verdict("SIM_CH_1", "quiet")
    assert calls == []


def test_pm_logger_not_called_on_phase_guard_or_unknown_chamber():
    """phase 가드(재전달·오상태)·미지 챔버 → 0회 (멱등: PM 사이클당 최대 1회)."""
    log, calls = _logger_mock()
    p, _, _, _ = _pmode(pm_logger=log)
    p.on_qual_verdict("SIM_CH_9", "loud")            # 미지 챔버
    p.on_pm("SIM_CH_1", 7, "2026-07-28T05:12:33+00:00")
    p.on_qual_verdict("SIM_CH_1", "loud")            # 1회 (PHASE_0 → PHASE_1)
    p.on_qual_verdict("SIM_CH_1", "loud")            # 재전달 — PHASE_0 아님 → 무시
    p.on_qual_verdict("SIM_CH_1", "loud")
    assert len(calls) == 1


def test_pm_logger_failure_does_not_block_phase_1():
    """★기입 실패해도 Phase 1 진입·광폭 seam 유지 (6-2 — pm_log는 안전 경로 아님)."""
    log, calls = _logger_mock()
    log.boom = True
    p, w, t, reloads = _pmode(pm_logger=log)
    _enter_loud(p)
    assert len(calls) == 1                           # 호출은 됐고 예외는 삼켜짐
    assert p.get_phase("SIM_CH_1") == Phase.PHASE_1
    assert w.calls == 1 and len(reloads) == 1 and t.excluded == {"SIM_CH_1"}


def test_pm_logger_called_after_widening():
    """호출 순서 — 광폭 write/reload/set_excluded **뒤**에 기입(광폭이 우선순위)."""
    order = []
    w_calls = []

    class _OrderTTTM:
        def set_excluded(self, s):
            order.append("excluded")

    p = ProvisionalMode(_cfg(), limits=_limits(),
                        writer=type("W", (), {"write_provisional":
                                              lambda self, rows: order.append("write")})(),
                        reload_fn=lambda: order.append("reload"),
                        tttm=_OrderTTTM(),
                        pm_logger=lambda payload: (order.append("pm_log"), w_calls.append(payload)))
    _enter_loud(p)
    assert order == ["write", "reload", "excluded", "pm_log"]


def test_on_pm_stores_detected_at_and_ts_optional():
    """`on_pm(ts)` 저장 + 미전달(None) 하위호환 — 기존 호출부(b63e·테스트) 무회귀."""
    p, _, _, _ = _pmode()
    p.on_pm("SIM_CH_1", 7, "2026-07-28T05:12:33+00:00")
    assert p._state["SIM_CH_1"]["pm_detected_at"] == "2026-07-28T05:12:33+00:00"
    p.on_pm("SIM_CH_2", 3)                           # ts 미전달(구 시그니처)
    assert p._state["SIM_CH_2"]["pm_detected_at"] is None


def test_pm_detected_at_resets_each_pm_cycle():
    """새 PM 사이클마다 개방 시각 갱신 — 직전 사이클 시각이 새 기입에 새지 않는다."""
    log, calls = _logger_mock()
    p, _, _, _ = _pmode(pm_logger=log)
    p.on_pm("SIM_CH_1", 7, "2026-07-28T05:00:00+00:00")
    p.on_qual_verdict("SIM_CH_1", "quiet")           # 조용 → NORMAL 복귀(기입 없음)
    p.on_pm("SIM_CH_1", 8, "2026-08-28T09:30:00+00:00")   # 새 사이클
    p.on_qual_verdict("SIM_CH_1", "loud")
    assert calls[0]["pm_detected_at"] == "2026-08-28T09:30:00+00:00"
    assert calls[0]["pm_count"] == 8


def test_no_pm_logger_is_noop():
    """pm_logger 미주입(기본 None) → 전이 정상·예외 없음 (b63e·기존 테스트 경로)."""
    p, w, _, _ = _pmode()                            # pm_logger 없음
    _enter_loud(p)
    assert p.get_phase("SIM_CH_1") == Phase.PHASE_1 and w.calls == 1


def test_restore_keeps_pm_detected_at_none():
    """재시작 복구는 개방 시각을 파생 못 함 → None. 복구 챔버는 이미 기입 완료라 재기입 없음."""
    log, calls = _logger_mock()
    p, _, _, _ = _pmode(pm_logger=log)
    p.restore({"SIM_CH_1": {"phase": Phase.PHASE_1, "post_pm_count": 0, "verdict": "loud",
                            "boundary": 1000, "last_pm_count": 5}})
    assert p._state["SIM_CH_1"]["pm_detected_at"] is None
    p.on_qual_verdict("SIM_CH_1", "loud")            # PHASE_1 — phase 가드로 무시
    assert calls == []
