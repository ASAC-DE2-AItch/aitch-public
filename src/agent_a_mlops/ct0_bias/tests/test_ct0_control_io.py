# -*- coding: utf-8 -*-
"""CT⓪ bias 파일 채널 테스트 — 원자 교체·가드 로드·축퇴 (설계 §8 장애 매트릭스).

검증 대상:
  · 왕복(write→read)과 mtime 캐시 재사용
  · 손상 파일·비 dict·NaN → **bias=0 축퇴** (raw 예측 유지)
  · 파일 부재 → 0, 재생성 시 자동 복귀
  · 읽기 실패(잠금 모사) → **직전 값 유지**
  · 어떤 입력에도 예외를 밖으로 내지 않는다 (예측 발행은 멈추지 않는다)

실행: python -m pytest src/agent_a_mlops/ct0_bias/tests/test_ct0_control_io.py -q
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ct0_bias import control_io  # noqa: E402


def _payload(bias):
    return control_io.bias_payload("SIM_CH_3", bias=bias, status="APPLIED", mode="active",
                                   n=231, raw_bias=bias, cap=99.84, ct_id="CT-x-R2R",
                                   valid_from_pm_count=25, train_rmse=99.84)


def test_write_read_roundtrip(tmp_path):
    control_io.write_bias("SIM_CH_3", _payload(12.34), tmp_path)
    cache = control_io.BiasCache(tmp_path)
    assert cache.get("SIM_CH_3") == 12.34


def test_atomic_replace_leaves_no_tmp(tmp_path):
    """tmp 잔여물이 남으면 다음 스캔이 부분 파일을 볼 위험이 생긴다."""
    p = control_io.write_bias("SIM_CH_3", _payload(1.0), tmp_path)
    assert p.exists() and not list(tmp_path.glob("*.tmp"))


def test_update_is_picked_up(tmp_path):
    """갱신 후 다음 조회에서 새 값이 보인다 (mtime+size 캐시 무효화)."""
    cache = control_io.BiasCache(tmp_path)
    control_io.write_bias("SIM_CH_3", _payload(5.0), tmp_path)
    assert cache.get("SIM_CH_3") == 5.0
    control_io.write_bias("SIM_CH_3", _payload(-7.25), tmp_path)
    assert cache.get("SIM_CH_3") == -7.25


def test_missing_file_is_zero(tmp_path):
    assert control_io.BiasCache(tmp_path).get("SIM_CH_9") == 0.0


def test_missing_after_present_falls_back_to_zero(tmp_path):
    """파일 삭제는 '보정 끄기' 수동 개입이기도 하다 — 직전 값을 붙들지 않는다."""
    cache = control_io.BiasCache(tmp_path)
    path = control_io.write_bias("SIM_CH_3", _payload(9.0), tmp_path)
    assert cache.get("SIM_CH_3") == 9.0
    path.unlink()
    assert cache.get("SIM_CH_3") == 0.0
    control_io.write_bias("SIM_CH_3", _payload(3.0), tmp_path)   # 재생성 → 복귀
    assert cache.get("SIM_CH_3") == 3.0


def test_corrupt_payloads_degrade_to_zero(tmp_path):
    """`json.loads` 성공 ≠ dict. 숫자·배열·null·NaN·필드 결손 전부 0 으로 (헌법 7장)."""
    cache = control_io.BiasCache(tmp_path)
    path = control_io.bias_path("SIM_CH_3", tmp_path)
    for body in ('{"bias": ', '123', '[1,2]', 'null', '{"bias": "abc"}',
                 '{"bias": NaN}', '{"n_samples": 3}'):
        path.write_text(body, encoding="utf-8")
        assert cache.get("SIM_CH_3") == 0.0, body


def test_read_failure_holds_last_value(tmp_path, monkeypatch):
    """읽기 예외(Windows 잠금 등)는 손상이 아니다 — 직전 값 유지 + backoff."""
    cache = control_io.BiasCache(tmp_path)
    control_io.write_bias("SIM_CH_3", _payload(4.0), tmp_path)
    assert cache.get("SIM_CH_3") == 4.0

    control_io.write_bias("SIM_CH_3", _payload(6.0), tmp_path)   # 값 변경(캐시 무효화 유발)

    def boom(_path):
        raise PermissionError("잠김")

    monkeypatch.setattr(control_io, "read_bias", boom)
    assert cache.get("SIM_CH_3") == 4.0                          # 6.0 이 아니라 직전 값


def test_get_never_raises(tmp_path, monkeypatch):
    """최후 방어선 — 어떤 예외도 예측 발행 경로로 새지 않는다 (설계 원칙 6)."""
    cache = control_io.BiasCache(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("예상 못한 실패")

    monkeypatch.setattr(control_io, "bias_path", boom)
    assert cache.get("SIM_CH_3") == 0.0


def test_write_rejects_nan(tmp_path):
    """NaN 은 표준 JSON 이 아니다 — 쓰기 길목에서 막는다 (읽는 쪽 예측이 NaN 이 된다)."""
    bad = _payload(0.0)
    bad["raw_bias"] = float("nan")
    try:
        control_io.write_bias("SIM_CH_3", bad, tmp_path)
    except ValueError:
        return
    raise AssertionError("NaN 이 파일로 나갔다")


def test_filename_is_sanitized(tmp_path):
    """챔버 id 의 경로 문자는 파일명으로 새지 않는다."""
    p = control_io.bias_path("../../etc/passwd", tmp_path)
    assert p.parent == tmp_path and ".." not in p.name


def test_payload_carries_audit_fields(tmp_path):
    """파일은 서빙값 1개 + 감사·진단 정보를 함께 싣는다 (운영 중 눈으로 볼 수 있게)."""
    control_io.write_bias("SIM_CH_3", _payload(2.0), tmp_path)
    d = json.loads(control_io.bias_path("SIM_CH_3", tmp_path).read_text(encoding="utf-8"))
    for key in ("bias", "status", "mode", "n_samples", "cap", "ct_id",
                "valid_from_pm_count", "updated_at", "schema"):
        assert key in d, key
