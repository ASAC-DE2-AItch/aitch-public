# -*- coding: utf-8 -*-
"""prior_seed(A26 · R9 승인 경유 시드) 핵심 검증 — DB·fastapi 무의존 (fake conn).

검증 항목:
  ① seed_payload 가 #152 ct0_bias/control_io.bias_payload 계약 키를 전부 갖춘다(동형)
     — 서빙(BiasCache)이 읽는 값은 `bias` 하나지만, 키 결손은 머지 후 import 교체를 깬다.
  ② '추정' 딱지(seed.estimate)·asymptote_only 필수 (신규 결정 9 — 라벨 없는 값의 정직 표기).
  ③ write_seed 원자 교체 — 생성·재쓰기(교체)·.tmp 잔재 0·NaN 거부(allow_nan=False).
  ④ bias_path 위생 — 챔버 문자열의 경로 문자를 파일명에 넣지 않는다.
  ⑤ build_proposal 조립 — +364 검산(−6.96 × −52.30), 자 일치→basisMismatch=False /
     scenario_def→True (주입 공개 원칙), 원장 τ·A 부재 시 폴백(12.5 / 491 — F6 이월).
  ⑥ prior_ledger._basis_pair — 자 파싱 fail-fast (자를 모르는 채 적합 금지 — 헌법 7장).
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from gateway import prior_seed  # noqa: E402

# #152 ct0_bias/control_io.bias_payload 키 집합 (계약 동결 — 머지 후 import 교체 후보)
CONTRACT_KEYS = {"schema", "chamber_id", "bias", "status", "mode", "n_samples",
                 "raw_bias", "cap", "ct_id", "valid_from_pm_count", "train_rmse",
                 "window_model_version", "updated_at"}


def _payload(**kw):
    base = dict(chamber_id="CH4", bias=364.008, approval_id="APR-PRS-1",
                marker_version="v1 · slope -6.96", basis="qual5-seg1 -> last1000_mean",
                delta=-52.30, valid_from_pm_count=0, tau_days=12.5, amplitude=491.0,
                ledger_ref="scenario:qual_loud")
    base.update(kw)
    return prior_seed.seed_payload(base.pop("chamber_id"), **base)


def test_contract_keys_superset():
    p = _payload()
    missing = CONTRACT_KEYS - set(p)
    assert not missing, f"#152 계약 키 결손: {missing}"
    assert p["schema"] == 1 and p["status"] == "SEED" and p["mode"] == "off"
    assert p["bias"] == 364.01 and p["raw_bias"] == 364.01      # round 2 (계약과 동일 규율)
    assert p["n_samples"] == 0 and p["cap"] is None             # 라벨 0건 · R9 상한 예외
    assert p["ct_id"] == "APR-PRS-1"                            # 기록 없으면 쓰기 없음


def test_estimate_flags():
    s = _payload()["seed"]
    assert s["estimate"] is True and s["asymptote_only"] is True
    assert s["tau_days"] == 12.5 and s["amplitude"] == 491.0    # 감쇠 몫은 메타로만 (자 혼합 금지)


def test_write_seed_atomic(tmp_path):
    out = prior_seed.write_seed(_payload(), tmp_path)
    assert out == tmp_path / "bias_CH4.json"
    got = json.loads(out.read_text(encoding="utf-8"))
    assert got["bias"] == 364.01 and got["status"] == "SEED"
    # 재쓰기 = 통째 교체 (BiasCache 는 mtime+size 캐시 — 부분 쓰기 노출 금지)
    prior_seed.write_seed(_payload(bias=100.0, approval_id="APR-PRS-2"), tmp_path)
    got2 = json.loads(out.read_text(encoding="utf-8"))
    assert got2["bias"] == 100.0 and got2["ct_id"] == "APR-PRS-2"
    assert not list(tmp_path.glob("*.tmp"))                     # 원자 교체 잔재 0


def test_write_seed_rejects_nan(tmp_path):
    p = _payload()
    p["bias"] = float("nan")                                    # 계약: NaN 금지 (allow_nan=False)
    with pytest.raises(ValueError):
        prior_seed.write_seed(p, tmp_path)
    assert not list(tmp_path.glob("*"))                         # 실패 시 아무것도 안 남는다


def test_bias_path_sanitized(tmp_path):
    p = prior_seed.bias_path("CH4/../evil", tmp_path)
    assert p.parent == tmp_path
    assert "/" not in p.name and "\\" not in p.name and ".." not in p.name


# ── build_proposal — psycopg2 커서 대역 (execute 순서대로 예약된 결과 반환) ────────
class _FakeCursor:
    def __init__(self, results):
        self._results = list(results)
        self._current = None

    def execute(self, sql, params=None):
        self._current = self._results.pop(0)

    def fetchone(self):
        return self._current

    def close(self):
        pass


class _FakeConn:
    def __init__(self, results):
        self._results = results

    def cursor(self):
        return _FakeCursor(self._results)


def _mk_conn(delta_basis="qual5-seg1", tau=None, amp=None):
    marker = ("v1", {"C17": -6.96}, "qual5-seg1 -> last1000_mean")
    tr = ("PRI-20260808-CH4-001", -52.30, delta_basis, tau, amp,
          364.0, None, "injected", "scenario:qual_loud", "2026-08-08T07:00:00Z",
          "INC-20260808-CH4-001")
    return _FakeConn([marker, tr, (3, 3)])


def test_build_proposal_matched_basis():
    prop = prior_seed.build_proposal("CH4", conn=_mk_conn())
    assert prop["seedBias"] == 364.0                            # −6.96 × −52.30 검산 (0013 주석과 동일)
    assert prop["estimate"] is True and prop["basisMismatch"] is False
    assert prop["template"]["tauDays"] == 12.5                  # 원장 τ 부재 → F6 폴백
    assert prop["template"]["amplitude"] == 491.0
    assert prop["ledger"] == {"n": 3, "injected": 3}
    assert prop["incidentId"] == "INC-20260808-CH4-001"         # 승인 행의 incident 참조


def test_build_proposal_flags_scenario_def():
    prop = prior_seed.build_proposal("CH4", conn=_mk_conn(delta_basis="scenario_def"))
    assert prop["basisMismatch"] is True                        # 숨기지 않는다 — 주입 공개 원칙
    assert "메커니즘" in prop["basisNote"]


def test_ledger_basis_pair_failfast():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "prior_ledger", REPO_ROOT / "scripts" / "prior_ledger.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._basis_pair("qual5-seg1 -> last1000_mean") == ("qual5-seg1", "last1000_mean")
    with pytest.raises(RuntimeError):
        mod._basis_pair("last1000_mean")                        # 자 한쪽 결손 — 즉시 예외
