# =============================================================================
# prediction_cache.py   —  [담당: A]  Step2
# =============================================================================
# 이 모듈은 wafer_id 별 최신 예측을 메모리에 보관하는 캐시 클래스
# `PredictionCache` 하나를 제공한다. 예측(fdc.prediction)과 SPC 신호는 서로
# 다른 시점에 도착하므로, 먼저 온 예측을 잠시 붙들어 두었다가 collector 가
# 조인할 때 꺼내 쓰기 위한 "임시 창고"다.
#
# [자료구조]
#   내부는 dict 하나: { wafer_id(str) -> entry(dict) }.
#   entry = { pred, drift, anomaly, shap, is_qual, ts, _read }
#     · pred/drift/anomaly/shap/is_qual : fdc.prediction 페이로드에서 뽑은 값
#     · ts     : 최신값 판정 기준 타임스탬프(메시지 자체 값 우선, 없으면 수신시각)
#     · _read  : 내부 플래그 — get() 으로 한 번이라도 조인됐는지 (get 반환 시 제거)
#
# [공개 메서드]
#   · upsert(msg)      : 예측 dict 를 캐시에 반영. wafer_id 기준 덮어쓰기이되,
#                        ts 가 기존보다 오래되면(순서 역전) 무시한다.
#   · get(wafer_id)    : 조인용 조회. 없거나 TTL 만료면 None, 있으면 **깊은 사본**을 반환
#                        (중첩 shap 참조 공유로 인한 원본 오염 차단 — 코드리뷰 §3-3).
#   · purge_expired()  : 만료 항목을 적극 회수(consumer 가 주기 호출).
#   · from_config(cfg) : params.yaml[prediction_cache] 로 인스턴스 생성.
#   · len(cache)       : 현재 보관 항목 수.
#
# [핵심 동작]
#   · 최신값 유지 : upsert 는 ts 비교로 늦게 온 오래된 메시지가 최신값을 덮지
#                   않도록 막는다.
#   · TTL/용량   : ttl_seconds 초과 항목은 만료 처리, max_entries 초과 시 ts 가
#                   가장 오래된 항목부터 축출한다(둘 다 None=비활성).
#   · 동시성     : upsert(쓰기)와 get(읽기)가 다른 스레드일 수 있어 threading
#                   .RLock 으로 모든 접근을 감싼다.
#   · 만료 로깅  : 조인 후 자연 만료는 DEBUG, 미조인 만료(예측만 오고 TTL 내
#                   조인 없음)는 이상 신호일 수 있어 WARNING 으로 남긴다.
#
# [경계 — 이 파일이 하지 않는 것]
#   · fdc.prediction 구독 루프는 없다 — 이미 역직렬화된 dict 를 입력으로 받는다
#     (구독 배선은 spc_consumer/agent_b_spc 소유. ★① ⓐ 확정).
#   · get() 이 None 일 때의 결측 정책은 caller(collector) 책임이다. 캐시는
#     None 만 돌려준다 (★③).
#   · 실측 C65 타겟은 담지 않는다 — 미래정보 누수 방지 (CLAUDE.md 1-3).
#
# [의존 계약·규칙]
#   · fdc.prediction 변경 금지 필드명을 그대로 읽는다: wafer_id, predicted_c65,
#     drift_score, shap_top3 (CLAUDE.md 2-1).
#   · TTL·용량 등 매직넘버 금지 → config/params.yaml[prediction_cache] (6-1).
#   · print 금지 → logging (6-1).
# =============================================================================
from __future__ import annotations

import copy
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("context_score.prediction_cache")

# fdc.prediction 계약 필드(변경 금지) — 캐시가 읽어들이는 원본 키. (CLAUDE.md 2-1)
_FIELD_WAFER_ID = "wafer_id"
_FIELD_PRED = "predicted_c65"
_FIELD_DRIFT = "drift_score"
_FIELD_SHAP = "shap_top3"
# 캐시 스키마상 함께 보관하는 부가 필드(계약 고정 대상 아님).
# anomaly_score = ae_score (ECDF 캘리 [0,1] · 0.2=VAL P98.5=Qual 임계) — D1 확정 2026-07-28,
#   Docs/score_ae_z발행_전환_변경점검_v1.md. 캐시는 값을 해석하지 않고 그대로 보관·반환한다.
_FIELD_ANOMALY = "anomaly_score"
_FIELD_IS_QUAL = "is_qual"
# 메시지가 자체 타임스탬프를 실어 보낼 때 허용하는 키(순서 역전 방어용). 없으면 수신시각.
_SOURCE_TS_KEYS = ("ts", "timestamp")


class PredictionCache:
    """wafer_id 별 최신 fdc.prediction 을 보관하는 thread-safe 캐시.

    쓰기(`upsert`)는 spc_consumer 가, 읽기(`get`)는 collector 가 호출한다.
    자료구조는 단순 dict + TTL(선택) + 용량 상한(선택)이며, 최신값 유지는
    ts 비교로 판정해 순서 역전 메시지가 최신값을 덮지 않도록 방어한다.

    Args:
        ttl_seconds: 항목 유효기간(초). None 이면 만료 없음.
        max_entries: 보관 항목 상한. None 이면 무제한. 초과 시 가장 오래된
            ts 항목부터 제거(용량 가드).
        clock: 현재시각(epoch 초) 공급 함수. 테스트 주입용(기본 time.time).
    """

    def __init__(
        self,
        ttl_seconds: Optional[float] = None,
        max_entries: Optional[int] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds 는 양수여야 합니다: {ttl_seconds}")
        if max_entries is not None and max_entries <= 0:
            raise ValueError(f"max_entries 는 양수여야 합니다: {max_entries}")
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._store: dict[str, dict] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, cfg: dict, clock: Callable[[], float] = time.time) -> "PredictionCache":
        """params.yaml 의 [prediction_cache] 섹션으로 캐시를 구성한다.

        키 부재 시 매직넘버 대체 없이 명시적으로 실패한다(헌법 6-1 — 조용한 기본값
        금지). ttl_seconds·max_entries 는 null 허용(각각 만료 없음·무제한 의미).
        """
        sec = cfg.get("prediction_cache")
        if sec is None:
            raise RuntimeError("config 에 [prediction_cache] 섹션이 없습니다")
        for key in ("ttl_seconds", "max_entries"):
            if key not in sec:
                raise KeyError(f"config [prediction_cache] 에 '{key}' 키가 없습니다")
        return cls(
            ttl_seconds=sec["ttl_seconds"],
            max_entries=sec["max_entries"],
            clock=clock,
        )

    def upsert(self, prediction_msg: dict) -> bool:
        """역직렬화된 fdc.prediction dict 를 wafer_id 기준 최신값으로 반영한다.

        ts 가 기존 항목보다 오래되면(순서 역전) 무시한다. wafer_id 가 없거나
        dict 형태가 아니면 스킵하고 로그만 남긴다(파이프라인 보호, 6-2).

        Returns:
            캐시가 실제로 갱신되면 True, 스킵/무시되면 False.
        """
        if not isinstance(prediction_msg, dict):
            log.warning("prediction 메시지가 dict 가 아님 → 스킵: type=%s", type(prediction_msg).__name__)
            return False

        wafer_id = prediction_msg.get(_FIELD_WAFER_ID)
        if wafer_id is None:
            log.warning("prediction 메시지에 %s 없음 → 스킵", _FIELD_WAFER_ID)
            return False
        wafer_id = str(wafer_id)

        ts = self._extract_ts(prediction_msg)
        entry = {
            "pred": prediction_msg.get(_FIELD_PRED),
            "drift": prediction_msg.get(_FIELD_DRIFT),
            "anomaly": prediction_msg.get(_FIELD_ANOMALY),
            "shap": prediction_msg.get(_FIELD_SHAP),
            "is_qual": prediction_msg.get(_FIELD_IS_QUAL),
            "ts": ts,
            "_read": False,   # 내부 플래그: get() 으로 조인된 적 있는지 (미조인 만료 감시용)
        }

        with self._lock:
            prev = self._store.get(wafer_id)
            if prev is not None and prev["ts"] > ts:
                log.debug(
                    "순서 역전 무시: wafer_id=%s 기존ts=%.3f > 수신ts=%.3f",
                    wafer_id, prev["ts"], ts,
                )
                return False
            self._store[wafer_id] = entry
            self._enforce_capacity()
        return True

    def get(self, wafer_id: str) -> Optional[dict]:
        """collector 조인용 조회. 없거나 TTL 만료면 None.

        반환값은 캐시 내부 dict 의 **깊은 사본**(내부 플래그 `_read` 제외)이라 호출측
        수정이 캐시에 새지 않는다. 성공 조회 시 해당 항목을 '조인됨'으로 표시한다.
        결측(None) 시 후속 정책은 collector/오케스트레이터 책임이다(★③).

        **deepcopy 인 이유** (코드리뷰 §3-3 실증): 1단계 사본은 `shap`(list of dict)
        같은 중첩 구조의 참조를 그대로 공유해, 하류(오케스트레이터·instrumentation·
        리포트)가 원소를 변조하면 **캐시 원본이 오염**된다. 같은 wafer 를 재조회하거나
        재전달로 다시 조인할 때 오염된 값이 나오고, 원인 추적은 조인 시점에서 멀어진다.
        비용은 무시 가능하다 — 항목당 스칼라 5개 + `shap_top3` 3원소(계약 §2).
        """
        key = str(wafer_id)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if self._is_expired(entry):
                del self._store[key]
                self._log_expiry(key, entry)
                return None
            entry["_read"] = True
            return {k: copy.deepcopy(v) for k, v in entry.items() if k != "_read"}

    def purge_expired(self) -> int:
        """만료 항목을 적극 제거한다. consumer 가 주기적으로 호출하는 청소 훅.

        get() 은 조회 시점에만 만료를 감지하므로, 한 번도 조인되지 않은 예측
        (stranded)은 용량 축출 전까지 방치될 수 있다. 이 메서드는 그런 항목을
        제때 회수하고, 미조인 만료는 WARNING 으로 남긴다(정상 만료는 DEBUG).

        Returns:
            제거한 항목 수.
        """
        with self._lock:
            expired = [w for w, e in self._store.items() if self._is_expired(e)]
            for w in expired:
                entry = self._store.pop(w)
                self._log_expiry(w, entry)
            return len(expired)

    def __len__(self) -> int:
        """현재 보관 항목 수(만료분 lazy 제거 전 기준)."""
        with self._lock:
            return len(self._store)

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────
    def _extract_ts(self, msg: dict) -> float:
        """메시지 자체 타임스탬프가 있으면 사용, 없으면 수신시각을 부여한다."""
        for k in _SOURCE_TS_KEYS:
            v = msg.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return self._clock()

    def _is_expired(self, entry: dict) -> bool:
        """TTL 설정 시 항목 만료 여부."""
        if self._ttl is None:
            return False
        return (self._clock() - entry["ts"]) > self._ttl

    def _log_expiry(self, wafer_id: str, entry: dict) -> None:
        """만료 제거 로그. 조인된 적 없는 미조인 만료만 WARNING(그 외 DEBUG).

        미조인 만료 = 예측은 수신됐으나 TTL 내 collector 조인이 없었던 항목 →
        SPC 측 미도달·조인 지연 등 이상 신호일 수 있어 표면화한다.
        """
        if entry.get("_read"):
            log.debug("TTL 만료 제거(조인 완료분): wafer_id=%s", wafer_id)
        else:
            log.warning(
                "예측 캐시 미조인 만료: wafer_id=%s ts=%.3f — 예측 수신 후 TTL(%ss) 내 조인 없음",
                wafer_id, entry["ts"], self._ttl,
            )

    def _enforce_capacity(self) -> None:
        """용량 상한 초과 시 ts 가 가장 오래된 항목부터 제거한다(lock 내부 호출).

        축출 대상이 조인된 적 없는 미조인 항목이면 WARNING 으로 남긴다 —
        용량 압박으로 예측이 조인 전에 밀려나는 상황은 상한 부족 신호일 수 있다.
        """
        if self._max_entries is None:
            return
        while len(self._store) > self._max_entries:
            oldest = min(self._store, key=lambda w: self._store[w]["ts"])
            entry = self._store.pop(oldest)
            if entry.get("_read"):
                log.debug("용량 상한 초과 제거(조인 완료분): wafer_id=%s", oldest)
            else:
                log.warning("용량 상한 초과로 미조인 예측 제거: wafer_id=%s", oldest)


def _repo_root() -> Path:
    """리포지토리 루트 경로(config/params.yaml 해석용)."""
    return Path(__file__).resolve().parents[3]
