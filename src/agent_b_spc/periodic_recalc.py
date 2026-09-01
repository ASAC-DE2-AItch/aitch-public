"""Step 7 S1 — 정기 리캘리 라이브 트리거 순수 헬퍼.

consumer(spc_consumer)가 쓰는 부작용 없는 조각만 모은다 — DB·Kafka·상태 없음.
스태거(D6-c)·baseline 맵 조립(D6-a)·resolution 조회(D3). DB 접촉(reset_ref)은 호출자가
주입(`reset_ref_fn`)해 여기선 순수 유지 → 단위테스트가 브로커·Postgres 불요.

스펙: Docs/B4-1_Step7_정기리캘리_라이브트리거_스펙플랜.md §3.
"""
from __future__ import annotations

from typing import Callable, Optional


def stagger_threshold(chambers: list, chamber: str, interval: int, *,
                      first_fired: bool) -> int:
    """챔버별 **첫 발동 임계만** 지연시켜 fleet median 동시 붕괴 방지 (D6-c).

    첫 발동: `interval + index·floor(interval/N)` (N=챔버 수) → 예: 4챔버·500이면 500·625·750·875.
    이후: `interval`(스태거 없음 — 락스텝 유지). chamber가 목록에 없으면 오프셋 0
    (`.index` ValueError 방어 — total function). 챔버 수 0(startup 전)은 `interval` 반환(0-division 방어).

    Args:
        chambers: 정렬된 챔버 목록(DB 유래 — `sorted({gk[0] for gk in limits})`).
        chamber: 대상 챔버.
        interval: 기본 텀블링 주기(`recalc_interval_wafers`).
        first_fired: 이 챔버가 첫 발동을 이미 마쳤는지.

    Returns:
        발동 임계(wafer 수).
    """
    if first_fired:
        return interval
    n = max(len(chambers), 1)
    try:
        idx = chambers.index(chamber)
    except ValueError:                      # 챔버 부재 — 오프셋 없이 기본 주기(total)
        idx = 0
    return interval + idx * (interval // n)


def build_baseline_map(group_keys, reset_ref_fn: Callable[[tuple], Optional[dict]]) -> dict:
    """각 group_key의 리셋 기준점을 모아 TTTM baseline 맵 생성 (D6-a).

    `reset_ref_fn(gk) -> {center, sigma_ref, limit_version} | None` 주입(운영=`reset_ref`+conn,
    테스트=fake). 기저 부재(None) 그룹은 **제외** → TTTM은 그 그룹에서 fallback(활성 행)을 쓴다.
    맵은 `tttm_engine.set_baseline`으로 주입돼 `sigma_ref`·`_reverse_rule`·`reference_id`가
    레짐 수립 행을 추종하게 한다(periodic·provisional 미추종, B4-2 결정 1).

    Args:
        group_keys: 대상 그룹 키 iterable((chamber, recipe, step, window, sensor)).
        reset_ref_fn: 그룹별 리셋 기준점 조회자(부작용은 이 fn 안에 격리).

    Returns:
        `{group_key: {center, sigma_ref, limit_version}}` — None 그룹 제외.
    """
    out: dict = {}
    for gk in group_keys:
        ref = reset_ref_fn(gk)
        if ref is not None:
            out[gk] = ref
    return out


def resolution_for(resolutions: dict, group_key: tuple) -> Optional[float]:
    """`group_key`의 σ-단위 자 분해능 조회 — 키 = chamber 제외 `group_key[1:]` (D3·리뷰 H1).

    `load_resolutions`(initial_limits) 산출물은 `(recipe, step, window, sensor)` 4-튜플 키라
    chamber를 떼고 조회한다(챔버 오프셋은 밴드 평행이동이라 분해능 무관). 미시딩 그룹은 None →
    `recompute_group`이 res_σ 하한 없이 처리.
    """
    return resolutions.get(tuple(group_key[1:]))
