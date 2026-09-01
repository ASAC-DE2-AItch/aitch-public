"""config/params.yaml 로드용 공용 헬퍼 — pandas/numpy 등 무거운 의존 없음.

`initial_limits`·`monitoring_whitelist`·`nelson_engine`이 공유한다. 순수·경량 엔진
(`nelson_engine`)이 데이터 모듈(`initial_limits` → pandas/numpy)에 결합하지 않도록 여기로
분리했다. 키 부재 시 매직넘버 대체 없이 명시적 실패(헌법 6-1).
"""

from __future__ import annotations


def _section(cfg: dict, name: str, path) -> dict:
    """params.yaml의 최상위 섹션을 반환. 부재 시 명시적 실패 (헌법 6-1)."""
    sec = cfg.get(name)
    if not sec:
        raise RuntimeError(f"config에 [{name}] 섹션이 없습니다: {path}")
    return sec


def _key(sec: dict, key: str, section: str, path):
    """섹션에서 키를 꺼낸다. 부재 시 누락 키 이름을 담아 실패 (헌법 6-1)."""
    if key not in sec:
        raise KeyError(f"config [{section}]에 '{key}' 키가 없습니다: {path}")
    return sec[key]
