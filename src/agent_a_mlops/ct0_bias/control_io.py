# -*- coding: utf-8 -*-
"""CT⓪ bias 전달 채널 — `control/ct0/bias_<chamber>.json` 원자 교체·가드 로드 (설계 D3).

방향은 **단방향**이다: `updater`(쓰기) → `consumer.py` 서빙(읽기). 서빙은 DB 를 만지지
않는다 — 보정 경로의 DB 장애가 예측 지연으로 전이되면 안 되기 때문이다(설계 §8).
선례 3개를 그대로 재사용한다: `control/ct1` promote 신호 채널 · tmp+`os.replace` 원자
교체 · mtime 캐시 (헌법 7장 "공유 JSON 파일을 열어놓고 제자리 덮어쓰기 금지").

읽기 측 축퇴 규율 (설계 §8 — 서로 다른 두 실패를 구분한다):
  · 파일 부재·손상·계약 위반  → **bias=0** (raw 예측 = 현행과 동일). 잘못된 값보다 무보정.
  · 읽기 자체가 실패(잠금 등) → **직전 값 유지** + 짧은 backoff. Windows 에서 쓰기 중
    잠깐 열리지 않는 것을 "손상"으로 오판해 보정을 껐다 켰다 하면 서빙값이 진동한다.

환경변수:
    CT0_BIAS_DIR        bias 파일 디렉토리 (기본 `<repo>/control/ct0`)
    CT0_BIAS_MAX_AGE_SEC  이 시간 넘게 갱신이 없으면 경고 1회 (기본 86400 · 0=비활성)
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("ct0-bias-io")

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_VERSION = 1
FILE_PREFIX = "bias_"
WRITE_RETRY = 5                    # Windows 잠금 대비 원자 교체 재시도 횟수
WRITE_RETRY_SLEEP = 0.05           # 재시도 간격(초) — 짧게, 총 0.25s 이내
READ_FAIL_BACKOFF_SEC = 1.0        # 읽기 실패 후 재시도까지 최소 간격 (바쁜 재시도 방지)
DEFAULT_MAX_AGE_SEC = 24 * 3600
# 점(.)까지 배제한다 — 슬래시만 막으면 `..` 이 파일명에 남아 로그·감사에서 경로 조작처럼
# 보이고, 챔버 id 는 원래 `SIM_CH_3` 꼴이라 점이 들어올 일이 없다.
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _env_max_age() -> float:
    """`CT0_BIAS_MAX_AGE_SEC` (기본 24h · 0=비활성). 잘못된 값은 기본값으로 흡수한다."""
    raw = os.environ.get("CT0_BIAS_MAX_AGE_SEC", "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_SEC
    try:
        v = float(raw)
    except ValueError:
        log.warning("CT0_BIAS_MAX_AGE_SEC=%r 판독 불가 → 기본값 %ds", raw, DEFAULT_MAX_AGE_SEC)
        return DEFAULT_MAX_AGE_SEC
    return v if v >= 0 else DEFAULT_MAX_AGE_SEC


def bias_dir() -> Path:
    """bias 파일 디렉토리 (env 우선 — 테스트·다중 인스턴스 격리용)."""
    env = os.environ.get("CT0_BIAS_DIR", "").strip()
    return Path(env) if env else REPO_ROOT / "control" / "ct0"


def bias_path(chamber_id: str, directory: Path | None = None) -> Path:
    """`bias_<chamber>.json`. 챔버 id 는 파일명 안전 문자만 남긴다(경로 주입 차단)."""
    safe = _SAFE.sub("_", str(chamber_id or "unknown"))
    return (directory or bias_dir()) / f"{FILE_PREFIX}{safe}.json"


# ── 쓰기 (updater 전용) ───────────────────────────────────────────────────
def write_bias(chamber_id: str, payload: dict, directory: Path | None = None) -> Path:
    """같은 디렉토리 tmp + `os.replace` 원자 교체 (헌법 7장).

    읽는 쪽이 mtime+size 캐시라 **부분 쓰기가 그대로 노출**되면 안 된다. Windows 는
    교체 순간 다른 프로세스가 파일을 열고 있으면 `PermissionError` 를 내므로 짧은
    backoff 재시도를 둔다 (같은 이유로 7장에 등재된 함정).
    """
    path = bias_path(chamber_id, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body.setdefault("schema", SCHEMA_VERSION)
    body.setdefault("chamber_id", chamber_id)
    body.setdefault("updated_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    # allow_nan=False: NaN 은 표준 JSON 이 아니라 읽는 쪽 `json.loads` 를 통과해도
    # 서빙 가산에서 예측을 통째로 NaN 으로 만든다 (헌법 7장).
    text = json.dumps(body, ensure_ascii=False, indent=2, allow_nan=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    last: Exception | None = None
    for i in range(WRITE_RETRY):
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            return path
        except OSError as e:                       # Windows 잠금·일시 IO 실패
            last = e
            time.sleep(WRITE_RETRY_SLEEP * (i + 1))
    tmp.unlink(missing_ok=True)                    # 잔여 tmp 정리 (다음 스캔이 볼 이유가 없다)
    raise OSError(f"bias 파일 원자 교체 실패({path}): {last}")


def bias_payload(chamber_id: str, *, bias: float, status: str, mode: str, n: int,
                 raw_bias: float | None, cap: float | None, ct_id: str | None,
                 valid_from_pm_count: int, train_rmse: float | None,
                 model_version: str | None = None) -> dict:
    """서빙이 읽는 계약 형태. 서빙이 실제로 쓰는 값은 `bias` 하나 — 나머지는 감사·진단용."""
    return {
        "schema": SCHEMA_VERSION,
        "chamber_id": chamber_id,
        "bias": round(float(bias), 2),
        "status": status,
        "mode": mode,
        "n_samples": int(n),
        "raw_bias": raw_bias,
        "cap": cap,
        "ct_id": ct_id,
        "valid_from_pm_count": int(valid_from_pm_count),
        "train_rmse": train_rmse,
        "window_model_version": model_version,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ── 읽기 (서빙 전용) ──────────────────────────────────────────────────────
def read_bias(path: Path) -> float | None:
    """파일 1개 → bias 값. 계약 위반이면 `None`(= 호출자가 0 으로 축퇴), 읽기 실패는 예외.

    `json.loads` 성공은 dict 를 뜻하지 않는다 — `"123"`·`[1,2]`·`null` 도 유효 JSON 이다
    (헌법 7장). 그래서 파싱 직후 `isinstance(dict)` 가드를 둔다.
    """
    raw = path.read_text(encoding="utf-8")          # OSError 는 호출자가 '읽기 실패'로 다룸
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        val = float(data.get("bias"))
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


class BiasCache:
    """서빙 측 bias 캐시 — mtime+size 가 바뀔 때만 다시 읽는다 (poll 루프에서 호출).

    `consumer.py` 는 이것 말고 CT⓪ 의 어떤 것도 import 하지 않는다 (설계 D3).
    """

    def __init__(self, directory: Path | None = None, max_age_sec: float | None = None):
        self.dir = directory or bias_dir()
        self.max_age = (_env_max_age() if max_age_sec is None else max_age_sec)
        self._values: dict[str, float] = {}         # chamber → 마지막 정상 bias
        self._stamps: dict[str, tuple] = {}         # chamber → (mtime, size)
        self._next_try: dict[str, float] = {}       # chamber → 읽기 실패 backoff 해제 시각
        self._warned: set = set()                   # 챔버별 경고 1회 (로그 폭주 방지)

    def get(self, chamber_id: str | None) -> float:
        """현재 bias (없으면 0.0). **어떤 예외도 밖으로 내지 않는다** — 예측 발행은
        보정 실패로 멈추지 않는다 (설계 원칙 6 · 헌법 6-2)."""
        if not chamber_id:
            return 0.0
        try:
            return self._get(str(chamber_id))
        except Exception as e:                      # noqa: BLE001 — 최후 방어선
            self._warn_once(f"{chamber_id}:unexpected", "bias 조회 예외 → 직전 값 유지: %s", e)
            return self._values.get(str(chamber_id), 0.0)

    def _get(self, chamber_id: str) -> float:
        path = bias_path(chamber_id, self.dir)
        try:
            st = path.stat()
        except FileNotFoundError:                   # 부재 → bias=0 축퇴 (설계 §8)
            # 사라진 파일을 "직전 값 유지"로 다루지 않는 이유: 파일 삭제는 보정을 끄는
            # 수동 개입 수단이기도 하다(롤백보다 더 급한 정지). 캐시를 비워 재생성 시
            # 정상 재적재되게 한다.
            self._stamps.pop(chamber_id, None)
            self._values[chamber_id] = 0.0
            return 0.0
        except OSError as e:                        # 잠금 등 — 직전 값 유지 + backoff
            return self._hold(chamber_id, e)

        # ⚠️ 나이 검사는 **캐시 히트보다 먼저** 한다. stamp 비교 뒤로 미루면, 파일이 굳어
        #   있을 때(= updater 사망) stamp 가 영원히 같아 검사 자체를 못 타고, 반대로 값이
        #   바뀐 직후엔 mtime 이 방금이라 조건이 성립하지 않는다 — 감시가 살아있는 척
        #   죽어 있는 형태가 된다 (spc_flags M1 사고와 같은 구조).
        self._check_age(chamber_id, st.st_mtime, path.name)

        stamp = (st.st_mtime, st.st_size)
        if self._stamps.get(chamber_id) == stamp:
            return self._values.get(chamber_id, 0.0)
        if time.monotonic() < self._next_try.get(chamber_id, 0.0):
            return self._values.get(chamber_id, 0.0)

        try:
            val = read_bias(path)
        except OSError as e:                        # 쓰기 중 잠김 — 손상이 아니다
            return self._hold(chamber_id, e)

        self._stamps[chamber_id] = stamp
        if val is None:                             # 손상·계약 위반 → 0 축퇴 (설계 §8)
            self._warn_once(f"{chamber_id}:corrupt",
                            "bias 파일 판독 불가(%s) → bias=0 축퇴 (raw 예측 유지)", path.name)
            self._values[chamber_id] = 0.0
            return 0.0
        self._warned.discard(f"{chamber_id}:corrupt")
        self._values[chamber_id] = val
        return val

    def _check_age(self, chamber_id: str, mtime: float, name: str) -> None:
        """파일 나이 감시 (§10 지표 '파일 스캔 나이'). 경고만 — 값은 건드리지 않는다.

        나이를 이유로 bias 를 0 으로 떨어뜨리지 않는 이유: 갱신이 없다는 것과 값이 틀렸다는
        것은 다르다. 라벨이 60일 지연이라 정상 운영에서도 며칠씩 갱신이 없을 수 있어,
        자동 해제로 만들면 서빙값이 오르내린다. 대신 사람이 볼 수 있게 남긴다.
        """
        if self.max_age <= 0:
            return
        age = time.time() - mtime
        key = f"{chamber_id}:stale"
        if age > self.max_age:
            self._warn_once(key, "bias 파일이 %.1f시간째 미갱신 (%s) — updater 생존 확인 필요",
                            age / 3600, name)
        else:
            self._warned.discard(key)

    def _hold(self, chamber_id: str, err: Exception) -> float:
        """읽기 실패 — 직전 값 유지 + 짧은 backoff (설계 §8 마지막 행)."""
        self._next_try[chamber_id] = time.monotonic() + READ_FAIL_BACKOFF_SEC
        self._warn_once(f"{chamber_id}:io", "bias 파일 읽기 실패 → 직전 값 유지: %s", err)
        return self._values.get(chamber_id, 0.0)

    def _warn_once(self, key: str, fmt: str, *args) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        log.warning(fmt, *args)

    def snapshot(self) -> dict[str, float]:
        """현재 캐시 값 (로그·진단용)."""
        return dict(self._values)
