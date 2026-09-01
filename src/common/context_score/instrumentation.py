# =============================================================================
# instrumentation.py   —  [담당: 공동]  Step4  · 계측 훅
# =============================================================================
# 스펙: Docs/B4-1_Step4_5_5b_통합구현계획서_v2.md M1-3·D-6 (원 스펙은
#       Docs/spec/_archive/B4-1_Step4_공통_orchestrator_combine_스펙플랜.md D6·D7).
#
# 역할: 축별 기여·결합 과정을 계측해 contribution_log(JSONL)로 덤프한다.
#
# [목적]
#   - §6 상관분석의 입력 자산: 각 wafer 의 s1..s4 기여와 최종 점수를 기록해두면
#     이후 grouping·가중치 산출(P1~P5)에 그대로 쓴다.
#   - 디버깅·검증: 어느 축이 알람을 견인했는지 추적.
#
# [기록 항목]  (D-6 — 축 단위. element(e1~e5) 확장은 Step 6)
#   wafer_id, chamber_id, ts,
#   s_spc, s_tttm, s_ae, s_pred,         # 축별 기여(할인 적용 후 = combine 입력)
#   raw_combined, final,                 # 결합·후처리 결과
#   is_transient, prediction_present     # 결측/할인 여부 플래그
#
# [부작용 격리]
#   이 모듈은 파일 IO 를 하므로 **예외를 던질 수 있다**. 호출자(오케스트레이터)가
#   `_log_contribution` 에서 try/except 로 감싸 계측 실패가 점수 반환을 막지 않게
#   한다(D-6). 여기서 조용히 삼키지 않는 이유는, 삼키면 계측이 죽은 걸 아무도
#   모른 채 Step 6 입력 자산이 비게 되기 때문이다 — 호출자가 warning 으로 표면화한다.
#
# [규칙]
#   - print 금지 → logging. 매직넘버/경로 하드코딩 금지 → params.yaml (6-1).
#   - 센서 C코드 원칙 (6-4). docstring 필수.
#   - config.py 는 불변(계획서 §6) → 경로는 이 모듈이 직접 params 에서 읽는다.
# =============================================================================
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

import yaml

from src.common.config_util import _key, _section   # 부재 시 명시 실패(헌법 6-1)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = REPO_ROOT / "config" / "params.yaml"

# contribution_log 1행의 필드 순서(고정) — Step 6 분석 스크립트가 이 키로 읽는다.
#   축 키(spc·tttm·ae·pred)는 `s_<축>` 으로 접두해 결합·후처리 필드와 구분한다.
LOG_FIELDS: tuple[str, ...] = (
    "wafer_id", "chamber_id", "ts",
    "s_spc", "s_tttm", "s_ae", "s_pred",
    "raw_combined", "final",
    "is_transient", "prediction_present",
)


@lru_cache(maxsize=1)
def _configured_log_path(config_path: Path = CONFIG_PATH) -> Path:
    """params.yaml `spc.context_score_contribution_log_path` → 절대 경로 (1회 캐시).

    상대경로면 리포 루트 기준으로 해석한다(실행 cwd 에 따라 로그가 흩어지지 않게).
    키 부재 시 매직넘버 대체 없이 명시적 실패(헌법 6-1) — 호출자 try/except 가 흡수한다.
    """
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    spc = _section(cfg, "spc", config_path)
    raw_str = str(_key(spc, "context_score_contribution_log_path", "spc", config_path))
    path = Path(raw_str)
    # POSIX 절대경로(/...)도 절대로 취급한다 — Windows 의 `Path.is_absolute()` 는 드라이브 문자가
    #   없으면 False 라, Linux 배포 설정(예: /var/log/...)이 Windows 개발기에서 REPO_ROOT 에
    #   잘못 붙는다. 배포 타깃이 Linux 이므로 슬래시 시작은 절대로 본다.
    is_abs = path.is_absolute() or raw_str.startswith(("/", "\\"))
    return path if is_abs else REPO_ROOT / path


def build_record(joined: dict, scores: dict, raw_combined: float, final: float) -> dict:
    """계측 1행(dict) 조립 — 순수·IO 없음 (테스트·다른 싱크에서 재사용).

    `scores` 는 **할인 적용 후**(= combine 입력) 값이다. 원본 축 산출과 할인분을
    구분해야 하면 Step 6 에서 필드를 추가한다(스키마는 additive 로 열어둔다).
    `joined` 접근은 전부 `.get()` — 키 결측이 계측을 죽이지 않게 한다.

    반환 dict 의 키 집합·순서는 `LOG_FIELDS` 와 일치한다(테스트로 강제 — 스키마 드리프트
    가드). 필드를 늘릴 땐 두 곳을 함께 고친다.
    """
    return {
        "wafer_id": joined.get("wafer_id"),
        "chamber_id": joined.get("chamber_id"),
        "ts": joined.get("ts"),
        "s_spc": scores.get("spc"),
        "s_tttm": scores.get("tttm"),
        "s_ae": scores.get("ae"),
        "s_pred": scores.get("pred"),
        "raw_combined": raw_combined,
        "final": final,
        "is_transient": bool(joined.get("is_transient", False)),
        "prediction_present": joined.get("prediction") is not None,
    }


def log_contribution(joined: dict, scores: dict, raw_combined: float, final: float,
                     path: Path | None = None) -> None:
    """축 기여 1행을 contribution_log(JSONL)에 append.

    파일은 append 모드로만 열어 기존 이력을 덮어쓰지 않는다(4-1 "덮어쓰지 않는다" 정신).
    디렉토리는 없으면 생성한다. 한글 서술이 섞일 수 있어 `ensure_ascii=False`.

    Args:
        joined: collector 산출 joined 이벤트.
        scores: 축 기여 dict(할인 적용 후 — combine 입력과 동일).
        raw_combined: combine 반환값(후처리 전).
        final: clip 까지 끝난 최종 점수.
        path: 덤프 경로. None 이면 params.yaml 설정값(테스트 주입용 인자).

    Raises:
        예외를 삼키지 않는다 — 호출자(오케스트레이터)가 격리·warning 한다(D-6).
    """
    target = path if path is not None else _configured_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(build_record(joined, scores, raw_combined, final),
                           ensure_ascii=False) + "\n")
