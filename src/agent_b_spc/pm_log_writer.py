"""B6-3-f: 요란(loud) 판정 확정 시 `pm_log.json` 런타임 기입 — a seam 실구현.

`ProvisionalMode`는 순수 코어라 파일 I/O를 모른다(`pm_logger` 주입 계약). 이 모듈이 그
seam을 실물로 채운다 — `provisional_seams`(DB seam)의 자매 모듈이며, 파일 I/O라 분리했다.

**왜 B가 쓰나 (D1)**: `QualVerdictConfirmed`(계약 §8-B)에는 `confirmed_at`(승인 시각)만 있고
**PM 개방 시각이 없다**. 개방 시각(pm_count↑ 감지 wafer의 timestamp)은 B 컨슈머만 관측하므로
`days_since_last_pm`의 기준점으로 가장 정확한 `date`를 쓸 수 있는 주체가 B다. → **작성자는
B 1곳, A는 읽기 전용**(이중 기입 방지 — 계약 §8-B-1).

⚠️ **단일 프로세스 전제**: `os.replace`는 *독자* 원자성만 보장한다. load→append→write에 락이
없으므로 `spc_consumer`가 **동시에 2개 이상 뜨면 lost update**가 난다(나중 쓰기가 앞 엔트리를
덮음). 계약 §8-B-1의 "작성자 = B 1곳"은 **프로세스 1개**를 포함한 전제다.

**소비측 계약**(A `lean85_pipeline`): `parse_pm_log`는 mtime+size 캐시라 기입 즉시 무효화되고,
`_parse_pm_entries`는 dict 엔트리에서 `date`·`type`만 읽고 **여분 키는 무시**한다(추적 필드
추가는 하위호환). `date`는 반드시 **naive UTC**여야 한다 — tz-aware를 섞으면 파서의 `sorted`
에서 aware/naive 비교 TypeError로 **A 추론 전체가 다운**된다(D3, CLAUDE.md 7장).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# pm_log `date` 포맷 — naive UTC 고정(D3). A `rows_to_trace`의 트레이스 축과 동일 규칙.
DATE_FMT = "%Y-%m-%d %H:%M:%S"
# 기입 주체 태그 — 단일 작성자(D1) 감사 흔적. 여분 키라 A 파서는 무시.
WRITTEN_BY = "spc_consumer"
# os.replace 재시도 — A가 매 wafer 이 파일을 stat/open 하므로 Windows에서 잠금 충돌 가능(§3-3).
#   총 대기 ≈ backoff×(1+2+3+4) = 1.0s. **`append`(진짜 새 요란 PM — 3~4개월에 1회) 전용 예산**:
#   드문 이벤트라 1초 물고 성공률을 사는 게 이득. 백그라운드 flush는 아래 참조.
REPLACE_RETRIES = 5
REPLACE_BACKOFF_SEC = 0.1
# 백그라운드 flush(poll 루프)는 **단발 시도·무대기**다 — 재시도 자체가 반복되므로 내부 backoff가
#   중복이고, 지속 잠금 시 poll마다 1초를 블로킹해 `fdc.raw` 핫패스를 초당 ~1건으로 주저앉힌다.
FLUSH_ATTEMPTS = 1
# 실패 후 쿨다운 — 지속 잠금에서 매 poll 파일시스템을 두드릴 이유가 없다(대상 이벤트가 분기 단위).
#   `append`(진짜 새 PM)는 쿨다운을 무시하고 전력 재시도한다.
FLUSH_COOLDOWN_SEC = 30.0
# pending 관리선(B7-1 D4) — 이 이상 쌓이면 "파일 쓰기 지속 실패" 이상 신호로 ERROR 1회(래치).
#   drop 하지 않는다(유실 유발). 이 상수는 __init__ 기본값(테스트·직접 생성용) 폴백이고,
#   운영 정본은 params `spc.pm_log_pending_cap`(resolve_pm_log_pending_cap, §4-3).
PENDING_CAP = 5
# ISO 파싱 실패 시 시도할 보조 포맷 (naive 문자열·offset 축약형 등 방어)
_FALLBACK_FMTS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                  "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y/%m/%d %H:%M:%S")


def to_naive_utc(value) -> str | None:
    """임의 시각 표현 → naive UTC `"%Y-%m-%d %H:%M:%S"` 문자열 (D3). 실패 시 None.

    tz-aware(예: `"2026-07-28T05:12:33.000+00:00"`·`...Z`·`...+0900`)는 **UTC로 변환 후 tz를
    제거**하고, naive는 이미 UTC wall-clock으로 간주해 그대로 포맷한다. 파싱 불가는 None →
    호출자가 fallback(그때는 `date_source`로 표시가 남는다).
    """
    if value is None:
        return None
    dt = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:                                    # py3.10은 'Z'를 못 읽어 명시 치환
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            for fmt in _FALLBACK_FMTS:
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
    if dt is None:
        logger.warning("pm_log 시각 파싱 실패 — fallback 진행: %r", value)
        return None
    if dt.tzinfo is not None:                   # ★aware → UTC 변환 후 tz 제거 (D3)
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime(DATE_FMT)


def _now_naive_utc() -> str:
    """현재 시각 naive UTC 문자열 (최종 fallback·`written_at` 소스)."""
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime(DATE_FMT)


def _as_int(value):
    """pm_count 정규화 — `"2"`(str)와 `2`(int)가 멱등 키에서 갈라지지 않게 (리뷰 L1)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class PmLogWriter:
    """`pm_log.json` 원자적 append writer — `ProvisionalMode(pm_logger=...)` seam 실구현.

    보증:
    - **원자성**: 같은 디렉토리 tmp에 쓰고 `os.replace` — A가 mtime+size 캐시로 읽는 중
      부분 쓰기가 노출되지 않는다.
    - **멱등**: `(chamber_id, pm_count, date)` 동일 엔트리가 있으면 skip. **date를 키에 포함**
      하는 이유(리뷰 H1): 시뮬레이터 `pm_count`는 프로세스마다 0에서 재시작하는데 pm_log는
      실행 간 누적되므로, `(chamber, pm_count)`만으로는 2회차 리허설의 **진짜 새 요란 PM이
      영구 미기입**된다. 방어 대상인 재전달은 같은 개방 시각을 실어오므로 date까지 일치한다.
    - **비파괴**: 파일이 손상(비-JSON·비-list)이면 **기입을 포기**한다 — 덮어쓰면 이력이
      소실되므로 복구는 수동(에러 로그만).
    - **유실 완화**: **모든 실패 경로**(원자 교체 실패·손상/읽기 실패)에서 엔트리를 `pending`
      에 보관하고 `flush_pending()`으로 재시도한다. 코어는 phase 가드로 재호출되지 않으므로
      큐가 없으면 그 PM은 A에 영원히 반영되지 않는다(리뷰 H2·H1). `flush_pending`은 컨슈머
      poll 루프가 상시 호출한다 — 다음 요란 PM은 3~4개월 뒤라 append 시점 재시도만으로는
      실전에서 재시도가 없는 것과 같다(리뷰 M1).
    - **핫패스 보호**: 백그라운드 재시도는 **단발·무대기 + 쿨다운**이다. 지속 잠금에서
      append용 재시도 예산(5회·총 ≈1.0s)을 그대로 쓰면 poll마다 1초를 블로킹해 `fdc.raw`
      소비가 초당 ~1건으로 주저앉는다 — 재시도 큐가 컨슈머 지연으로 전이되는 구조(2차 리뷰 M).
    - **무해 실패**: 모든 실패 경로가 예외 대신 False 반환/로그다. 코어 쪽도 try-except로
      한 번 더 격리하므로 기입 실패가 Phase 전이·컨슈머를 죽이지 않는다(6-2).
    """

    def __init__(self, path, *, retries: int = REPLACE_RETRIES,
                 backoff_sec: float = REPLACE_BACKOFF_SEC, written_by: str = WRITTEN_BY,
                 cooldown_sec: float = FLUSH_COOLDOWN_SEC, pending_cap: int = PENDING_CAP,
                 clock=time.monotonic, sleep=time.sleep) -> None:
        """`path`=pm_log.json 경로(정본 = 리포 루트 — params `pm_log.path`, §3-5).

        `clock`·`sleep`은 쿨다운·backoff 테스트용 주입점(컨슈머 `clock=time.monotonic` 패턴).
        전역 `time.sleep`을 patch하면 pytest 내부와 충돌하므로 주입으로 계측한다.
        """
        self.path = Path(path)
        self.retries = max(1, int(retries))      # 0·음수 주입 시 조용한 무동작 방지(리뷰 L3)
        self.backoff_sec = backoff_sec
        self.written_by = written_by
        self.cooldown_sec = cooldown_sec
        self.pending_cap = max(1, int(pending_cap))   # B7-1 D4 관리선 (0·음수 방지)
        self.clock = clock
        self.sleep = sleep
        self.pending: list = []                  # 실패분 — 다음 기회에 재시도(H1·H2)
        self._last_error: str | None = None      # 동일 실패 반복 시 로그 도배 억제
        self._pending_cap_warned = False         # B7-1 D4 — cap 초과 ERROR 래치(`_last_error`와 독립.
                                                 #   write/load 실패의 _log_once와 슬롯 공유 시 핑퐁으로
                                                 #   래치가 무력화되므로 전용 플래그, 리뷰 3a)
        self._cooldown_until: float = 0.0        # 이 시각 전에는 백그라운드 flush no-op

    # ── seam 진입점 ────────────────────────────────────────────────
    def append(self, payload: dict) -> bool:
        """요란 PM 1건 기입 (seam 시그니처). 기입=True / skip·실패=False.

        `payload` = 코어가 넘기는 `{chamber_id, pm_count, pm_detected_at, verdict}`.
        `date` 우선순위(D2·§3-4): `pm_detected_at`(PM 개방 시각 — 정본) → `confirmed_at`
        (코어가 이벤트 메타를 넘기게 되면 — 현재 미배선) → 기입 시각. 셋 다 동일 naive UTC
        변환을 거치며, 어느 쪽을 썼는지는 엔트리 `date_source`에 남는다(리뷰 M2).

        **요란만 기입**한다 — `verdict`가 loud가 아니면 거부(규약 방어선 이중화, 리뷰 L2).
        """
        verdict = payload.get("verdict")
        if verdict != "loud":
            logger.warning("pm_log는 요란(loud) PM만 기입 — 거부: verdict=%r chamber=%s",
                           verdict, payload.get("chamber_id"))
            return False
        self.flush_pending(force=True)           # 직전 실패분 먼저 (순서 보존·전력 재시도)
        return self._append_entry(self._build_entry(payload))

    __call__ = append                            # `pm_logger=PmLogWriter(p)` 직접 주입도 허용

    def flush_pending(self, force: bool = False) -> int:
        """보관된 실패 엔트리 재시도 → 성공 건수. 실패분은 pending에 남는다(H1·H2).

        **두 가지 호출 성격을 구분**한다:
        - `force=False` (컨슈머 poll 루프 — 상시): 쿨다운 중이면 no-op, 시도는 **단발·무대기**.
          지속 잠금이 핫패스(`fdc.raw` 소비) 지연으로 전이되지 않게 하는 게 목적이다.
        - `force=True` (`append` — 진짜 새 요란 PM): 쿨다운 무시 + 전체 재시도 예산 사용.
          분기에 1번뿐인 이벤트라 여기서는 대기를 감수하고 성공률을 산다.
        """
        if not self.pending:
            return 0                             # O(1) — 상시 호출 비용 없음
        if not force and self.clock() < self._cooldown_until:
            return 0                             # 쿨다운 — 파일시스템 재두드림 억제
        queued, self.pending, ok = self.pending, [], 0
        for entry in queued:
            if self._append_entry(entry, background=not force):
                ok += 1
        if ok:
            logger.info("pm_log 보류분 재기입 성공 %d건 (잔여 %d)", ok, len(self.pending))
        if len(self.pending) <= self.pending_cap:
            self._pending_cap_warned = False         # B7-1 D4 — cap 이하 회복 → 다음 초과에 ERROR 재무장
        return ok

    # ── 내부 ──────────────────────────────────────────────────────
    def _queue(self, entry: dict, background: bool, why: str) -> None:
        """실패 엔트리를 pending에 보관 + 쿨다운 설정 (모든 실패 경로 공통 — 리뷰 H1).

        보관 대상은 "요란 PM 1건"이라 개수는 PM 발생 수와 같다 → 무한 성장 우려 없음.
        `background`(poll 루프가 상시 돌리는 재시도분)는 DEBUG로 낮춘다 — 실패는 수동 복구
        전까지 지속 상태라, 매 poll ERROR를 찍으면 로그가 도배되고 정작 최초 실패가 묻힌다.
        """
        self.pending.append(entry)
        self._cooldown_until = self.clock() + self.cooldown_sec
        log = logger.debug if background else logger.error
        log("%s — 재시도 대기(pending=%d): chamber=%s pm_count=%s date=%s",
            why, len(self.pending), entry.get("chamber_id"), entry.get("pm_count"),
            entry.get("date"))
        # B7-1 D4 — pending 관리선 초과 = "파일 쓰기 지속 실패" 이상 신호. drop 하지 않고 ERROR
        #   **1회만**(전용 플래그 래치 — flush로 cap 이하 회복 시 재무장, §4-2). 정상이면 0 근처라
        #   여기 도달 자체가 조사 대상. ⚠️ `_log_once`(_last_error 슬롯)를 쓰지 않는다 — write/load
        #   실패의 `_log_once`와 슬롯을 핑퐁하면 매 poll ERROR가 반복돼 래치가 깨진다(리뷰 3a).
        if len(self.pending) > self.pending_cap:
            if not self._pending_cap_warned:
                self._pending_cap_warned = True
                logger.error("pm_log pending %d > cap %d — 파일 쓰기 지속 실패 의심(drop 없음, 조사 요망)",
                             len(self.pending), self.pending_cap)
            else:
                logger.debug("pm_log pending %d > cap %d (지속)", len(self.pending), self.pending_cap)

    def _log_once(self, key: str, msg: str, *args, exc_info: bool = False) -> None:
        """같은 실패가 반복되면 최초 1회만 상세 로그 (poll 루프 재시도 도배 억제).

        실패 종류가 바뀌거나 한 번 성공하면(`_last_error=None`) 다시 상세를 남긴다.
        """
        if self._last_error == key:
            logger.debug(msg, *args)
            return
        self._last_error = key
        logger.error(msg, *args, exc_info=exc_info)

    def _build_entry(self, payload: dict) -> dict:
        """payload → pm_log 엔트리. `type="major"`(D4 — 요란↔대PM 기존 규약)."""
        date, source = to_naive_utc(payload.get("pm_detected_at")), "pm_detected_at"
        if date is None:
            date, source = to_naive_utc(payload.get("confirmed_at")), "confirmed_at"
        if date is None:                         # 재시작·파싱 실패 → 기입 시각(출처 표시 필수)
            date, source = _now_naive_utc(), "written_at_fallback"
            logger.warning("pm_log: PM 개방 시각 미보유·파싱 실패 — 기입 시각으로 대체 "
                           "(chamber=%s, date_source=%s)", payload.get("chamber_id"), source)
        return {"date": date,
                "type": "major",                 # D4 — 판정 의미는 verdict가 운반
                "verdict": "loud",               # 요란만 기입 (위 가드 통과분)
                "chamber_id": payload.get("chamber_id"),
                "pm_count": _as_int(payload.get("pm_count")),
                "date_source": source,           # 여분 키(A 무시) — fallback 엔트리 식별용
                "written_by": self.written_by,
                "written_at": _now_naive_utc()}

    def _append_entry(self, entry: dict, background: bool = False) -> bool:
        """1건 기입 시도. `background`=poll 루프 재시도(단발·무대기·로그 억제)."""
        attempts = FLUSH_ATTEMPTS if background else self.retries
        entries = self._load()
        if entries is None:                      # 손상·읽기 실패 → 원본 보존 + **엔트리는 보관**
            # 손상은 지속 상태라 재시도가 당장은 계속 실패하지만, pending에 남아 있어야
            # 파일을 수동 복구한 시점에 살아난다. 버리면 코어가 재호출되지 않아 영구 유실
            # (리뷰 H1 — flush_pending이 pop한 보관분까지 함께 소멸하던 경로).
            self._queue(entry, background, "pm_log 읽기·파싱 실패로 기입 보류")
            return False
        if self._is_duplicate(entries, entry):
            logger.info("pm_log 중복 skip: chamber=%s pm_count=%s date=%s",
                        entry["chamber_id"], entry["pm_count"], entry["date"])
            return False
        entries.append(entry)
        if not self._atomic_write(entries, attempts):   # H2 — 교체 실패도 보관 후 재시도
            self._queue(entry, background, "pm_log 기입 보류(원자 교체 실패)")
            return False
        # ★기입 **성공** 시점에만 실패 상태를 푼다(2차 리뷰 M). 읽기 성공에서 풀면
        #   "읽히지만 교체만 계속 실패"(Windows 편집기·AV 잠금 — 이 재시도가 겨냥한 바로 그
        #   상황)에서 매 flush 리셋 → 억제가 무력화돼 poll마다 ERROR+traceback이 찍힌다.
        self._last_error = None
        self._cooldown_until = 0.0
        logger.info("pm_log 기입: date=%s(%s) chamber=%s pm_count=%s → %s",
                    entry["date"], entry["date_source"], entry["chamber_id"],
                    entry["pm_count"], self.path)
        return True

    def _load(self) -> list | None:
        """현행 엔트리 로드. 파일 부재=[] / 손상(비-JSON·비-list)=None(**보류** 신호 — H1).

        `json.loads` 성공이 list를 뜻하지 않는다 — `"123"`·`{}`·`null`도 유효 JSON이라
        `isinstance` 가드가 필수다(CLAUDE.md 7장).
        """
        if not self.path.exists():
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:                # exists()~open 사이 소멸(TOCTOU) — 부재와 동일
            return []
        except (OSError, ValueError) as exc:
            self._log_once(f"load:{type(exc).__name__}",
                           "pm_log 읽기·파싱 실패 — 원본 보존·기입 보류(수동 복구 필요): %s",
                           self.path, exc_info=True)
            return None
        if not isinstance(data, list):           # 7장: 파싱 성공 ≠ 기대 타입
            self._log_once("load:not-list",
                           "pm_log가 배열이 아님(%s) — 원본 보존·기입 보류: %s",
                           type(data).__name__, self.path)
            return None
        return data                              # ※실패 상태 해제는 **기입 성공** 시점(위 M)

    @staticmethod
    def _is_duplicate(entries: list, entry: dict) -> bool:
        """멱등 키 `(chamber_id, pm_count, date)` — dict 엔트리만 비교(legacy 문자열 무시).

        `pm_count`는 int 정규화해 비교한다(`"2"` vs `2`가 갈라지면 이중 기입 — 리뷰 L1).
        """
        ch, cnt, date = entry.get("chamber_id"), entry.get("pm_count"), entry.get("date")
        if ch is None or cnt is None:            # 키 불완전 → 중복 판정 불가(기입 진행)
            return False
        return any(isinstance(e, dict) and e.get("chamber_id") == ch
                   and _as_int(e.get("pm_count")) == cnt and e.get("date") == date
                   for e in entries)

    def _atomic_write(self, entries: list, attempts: int | None = None) -> bool:
        """같은 디렉토리 tmp → `os.replace`. Windows 잠금 대비 backoff 재시도(§3-3).

        `attempts`=1(백그라운드 flush)이면 **대기 없이 단발** — 재시도는 다음 poll이 맡는다.
        """
        attempts = self.retries if attempts is None else max(1, int(attempts))
        try:
            body = json.dumps(entries, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        except (TypeError, ValueError):
            logger.exception("pm_log 직렬화 실패 — 기입 포기: %s", self.path)
            return False
        tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())             # replace 전 디스크 반영(부분 쓰기 방지)
            for attempt in range(1, attempts + 1):
                try:
                    os.replace(tmp, self.path)   # 원자적 교체 — A의 부분 읽기 차단
                    return True
                except OSError:                  # Windows PermissionError 등 일시 잠금
                    if attempt == attempts:
                        raise                    # attempts=1이면 대기 없이 즉시 (핫패스 보호)
                    self.sleep(self.backoff_sec * attempt)
        except OSError as exc:
            self._log_once(f"write:{type(exc).__name__}", "pm_log 쓰기·교체 실패: %s",
                           self.path, exc_info=True)
            return False
        finally:
            if tmp.exists():                     # 실패 시 tmp 잔존 정리
                try:
                    tmp.unlink()
                except OSError:
                    logger.warning("pm_log tmp 정리 실패(수동 삭제 필요): %s", tmp)
        return False


def resolve_pm_log_path(config_path=None) -> Path:
    """params.yaml `pm_log.path`(리포 루트 상대) → 절대 경로 (§3-5, 매직값 금지 6-1).

    키 부재 시 대체값 없이 실패한다(config_util 관례) — **호출자가 폴백 책임**을 진다
    (`_build_provisional`은 예외를 잡아 `pm_logger=None`으로 기동한다, 리뷰 M3).
    """
    import yaml

    from src.common.config_util import _key, _section

    root = Path(__file__).resolve().parents[2]
    path = Path(config_path) if config_path else root / "config" / "params.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    rel = _key(_section(cfg, "pm_log", path), "path", "pm_log", path)
    p = Path(rel)
    return p if p.is_absolute() else root / p


def resolve_pm_log_pending_cap(config_path=None) -> int:
    """params.yaml `spc.pm_log_pending_cap` → int (B7-1 D4·§4-3, 매직값 금지 6-1).

    키 부재 시 대체값 없이 raise한다(config_util 관례). 호출자(`_build_provisional`)는
    **cap 해석만 별도 try로 감싸** 실패 시 `PmLogWriter` 기본값(`PENDING_CAP`)으로 폴백하고
    pm_log 기입은 유지한다 — cap은 비필수 튜닝값이라 path 해석 실패(pm_logger=None)와 달리
    pm_log 전체를 끄지 않는다(리뷰 3b).
    """
    import yaml

    from src.common.config_util import _key, _section

    root = Path(__file__).resolve().parents[2]
    path = Path(config_path) if config_path else root / "config" / "params.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return int(_key(_section(cfg, "spc", path), "pm_log_pending_cap", "spc", path))


def shadow_copy_dirs() -> list:
    """A `find_file`이 **리포 루트보다 먼저** 뒤지는 디렉토리 목록 (lean85_pipeline :118-125).

    탐색 순서 = `lean85/` → `lean85/frozen/` → `src/agent_a_mlops/` → `src/agent_a_mlops/문제1(하)/`
    → **REPO_ROOT**. 앞 4곳 중 어디든 `pm_log.json` 사본이 있으면 A는 루트를 안 본다.
    """
    root = Path(__file__).resolve().parents[2]
    a_root = root / "src" / "agent_a_mlops"
    return [a_root / "lean85", a_root / "lean85" / "frozen", a_root, a_root / "문제1(하)"]


def warn_if_shadow_copy(pm_log_path, dirs=None) -> list:
    """기동 가드 — 루트보다 먼저 집히는 **사본**이 있으면 경고 (§3-5). 반환=발견 경로 목록.

    사본이 생기면 **B가 쓰는 파일과 A가 읽는 파일이 갈라진다**(split-brain — .gitignore가
    `lean85/pm_log.json` 사본 출현을 예고). 발견 시 경고만(차단 아님).
    ⚠️ A 운영 경로의 1순위는 `LEAN85_PM_LOG` env라, env가 다른 파일을 가리키면 이 가드는
    잡지 못한다 — env 정리는 A 후속(계약 §8-B-1 "정본 = 루트").
    """
    found = [d / "pm_log.json" for d in (dirs or shadow_copy_dirs())
             if (d / "pm_log.json").exists()]
    for p in found:
        logger.warning("pm_log 사본 감지 — A find_file이 루트보다 먼저 집습니다"
                       "(split-brain 위험): %s (B 기입 대상=%s)", p, pm_log_path)
    return found
