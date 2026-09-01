# -*- coding: utf-8 -*-
"""Supervisor 판정 **평가 하네스** (C5-3 / C5-2 환각 튜닝).

pytest 와 다른 도구다:
    pytest        : 통과/실패 (이분법·결정적·빌드 차단)  — 내 코드가 맞나
    평가 하네스   : 점수·분포 (LLM 이라 흔들림·차단 안 함) — LLM 판단 품질이 좋나

**LLM 판정은 pass/fail 로 못 잰다.** "29건 중 23건 맞음, escalate 를 baseline_aging 으로
3번 착각" 처럼 분포로 봐야 프롬프트를 어디 고칠지 알 수 있다.
환각 제어 튜닝 = 프롬프트 수정 → 이 하네스로 점수 측정 → 다시 수정, 의 루프.

정답지 = **fixture 파일명**. `01_baseline_aging_below_gate.json` → 기대 verdict = baseline_aging.
4지선다 × 6단계 = 24건 + 엣지 5건(정답 없음 — 관찰만) + 파손 1건(스킵).

사용:
    python eval_supervisor.py                      # Mock 백엔드로 전체 (수초)
    python eval_supervisor.py --sample-per-verdict 1  # 판정별 1건 = 4건 (튜닝 루프용)
    python eval_supervisor.py --only equipment_fault  # 특정 판정만
    python eval_supervisor.py --real                  # 실 LLM (EC2 필요)
    python eval_supervisor.py --real --resume         # 중단분 이어서
    python eval_supervisor.py --read                  # **실제 서술 읽기** (LLM 재호출 없음)
    python eval_supervisor.py --read --only-wrong     # 오판정 건만 — 프롬프트 튜닝용

⚠️ 실 LLM 은 29건 × 4호출 = 116회, 7~10분. 안전장치:
   ① 부분 실행  ② 증분 저장(--resume)  ③ 동시 제한  ④ Mock 먼저
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

SERVICE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_ROOT.parents[1]))
sys.path.insert(0, str(SERVICE_ROOT / "tests"))

from src.agent_service.app.config import load_settings  # noqa: E402
from src.agent_service.app.llm.factory import make_backend  # noqa: E402
from src.agent_service.app.pipeline import handle_alert_with_brief  # noqa: E402
from src.agent_service.app.schemas.report import NONE_EXECUTABLE, VERDICT_TO_OPTION  # noqa: E402
from src.agent_service.app.source import iter_replay_alerts  # noqa: E402

logger = logging.getLogger("eval_supervisor")

FIXTURES = SERVICE_ROOT / "fixtures" / "alerts"
RESULTS = SERVICE_ROOT / "notes" / "eval" / "supervisor_results.jsonl"
# 컬럼 설명 사이드카 — 뷰어(notes/jsonl_viewer.html)가 읽어 헤더 툴팁으로 띄운다.
# 단일 소스 = 아래 COLUMN_DOCS. 매 실행 write_column_docs() 로 자동 갱신되므로 stale 되지 않는다.
COLUMNS_META = RESULTS.with_name("supervisor_results.columns.json")
# 라운드별 요약 누적 — 프롬프트 튜닝 추이(정확도·판정별)를 한 파일에서 본다(뷰어로 열면 라운드=행).
RUNS_LOG = RESULTS.with_name("runs_history.jsonl")


def fixtures_dir(args) -> Path:
    """평가에 쓸 fixture 디렉토리 — `--fixtures` 미지정이면 기본 `fixtures/alerts`.

    W9(fixture 다양성)에서 `alerts_v2/` 를 **병행** 운용하기 위한 것이다. 기본값을 바꾸지 않으므로
    기존 라운드(round3~12)는 그대로 재현된다 — 같은 모델·같은 코드로 두 세트를 재야 대조가 된다.

    경로가 없거나 `*.json` 이 0건이면 즉시 종료한다. 오타 하나로 "평가 대상 0건"인 채 라운드가
    '성공'해버리면 측정이 조용히 무효가 되기 때문이다(EC2 요금은 그대로 나간다).
    """
    if not getattr(args, "fixtures", None):
        return FIXTURES
    d = Path(args.fixtures).expanduser()
    if not d.is_dir():
        raise SystemExit(f"--fixtures 가 디렉토리가 아니다: {d}")
    if not any(d.glob("*.json")):
        raise SystemExit(f"--fixtures 에 *.json 이 없다: {d}")
    return d


def results_path(tag: Optional[str]) -> Path:
    """--tag 지정 시 라운드별 결과 파일(supervisor_results_<tag>.jsonl) — 덮어쓰기 방지·추이 비교."""
    name = f"supervisor_results_{tag}.jsonl" if tag else "supervisor_results.jsonl"
    return SERVICE_ROOT / "notes" / "eval" / name


def append_run_history(tag: Optional[str], summary: dict, *, real: bool,
                       provenance: Optional[dict] = None) -> None:
    """이번 실행 요약을 runs_history.jsonl 에 한 줄 누적 — 라운드 간 추이 비교용.

    `provenance` = **그 라운드가 무엇을 재료로 썼는가** (2026-08-05 신설). `real` 은 LLM 이
    실물인지만 말하고 정량 엔진·fixture 는 말하지 않는다. 그래서 round11~15 가 전부
    `AGENT_API_MODE=mock`(정량 = 스텁)으로 돌았는데도 기록만 봐서는 알 수가 없었고,
    `round12-realengine` 이라는 태그까지 붙어 **다섯 라운드를 실 엔진 측정으로 오해했다.**
    태그는 사람이 붙이는 이름이라 틀릴 수 있다 — 모드는 코드가 적는다.
    """
    row = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
           "tag": tag or "(untagged)", "real": real, **(provenance or {}), **summary}
    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUNS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.info("추이 기록 → %s (tag=%s)", RUNS_LOG.name, tag or "-")

VERDICTS = ("equipment_fault", "process_shift", "baseline_aging", "escalate")
_FALLBACK_MARK = "LLM 분석 실패"
# §4 규칙8이 지시하는 '빈 근거 표지' 원문. ⚠️ "없음" 부분문자열로 세면 안 된다 —
# 정상 근거에도 "급변·anomaly 없음", "손잡이 없음" 처럼 자연스럽게 들어간다(2026-07-21 오카운트 13건 실측).
_NO_EVIDENCE_MARK = "해당 근거 없음"


# --- 정답지 ---------------------------------------------------------------------
def expected_verdict(stem: str) -> Optional[str]:
    """fixture 파일명에서 기대 verdict 를 뽑는다. 엣지 케이스는 None(정답 없음 — 관찰만)."""
    for v in VERDICTS:
        if v in stem:
            return v
    return None


# --- 백엔드 ---------------------------------------------------------------------
# 2026-07-21 실측: 라운드6 도중 스팟 인스턴스가 회수돼 마지막 4건이 전부 fallback 으로
#   기록됐다. 엔드포인트가 없는데도 러너가 끝까지 돌아 '실패한 측정'이 '나쁜 성적'처럼
#   남는 게 문제다. 시작 전에 한 번 찔러 보고, 없으면 아예 시작하지 않는다.
PREFLIGHT_TIMEOUT_S = 10
# base_url 은 호스트까지만("http://host:8000") — 경로는 백엔드가 붙인다(vllm.py 규약).
PREFLIGHT_PATH = "/v1/models"


def _preflight(base_url: str | None) -> None:
    """실측 시작 전 LLM 엔드포인트 생존 확인. 죽어 있으면 측정을 시작하지 않는다."""
    if not base_url:
        raise SystemExit("AGENT_LLM_BASE_URL 미설정 — 실측을 시작할 수 없다.")
    url = base_url.rstrip("/") + PREFLIGHT_PATH
    try:
        with httpx.Client(timeout=PREFLIGHT_TIMEOUT_S) as client:
            client.get(url).raise_for_status()
    except Exception as exc:  # 연결·타임아웃·4xx/5xx 모두 '측정 불가'로 같다
        raise SystemExit(
            f"LLM 엔드포인트 응답 없음 ({type(exc).__name__}) — {url}\n"
            "  인스턴스가 살아 있는지 확인해라 (스팟이면 회수됐을 수 있다):\n"
            "  aws ec2 describe-instances --instance-ids <id> "
            '--query "Reservations[].Instances[].{state:State.Name,reason:StateReason.Code}"'
        ) from exc
    logger.info("preflight OK — %s", url)


#: 파이프라인이 근거를 길어오는 KB 컬렉션 (논리명 — 물리명은 kb_schema 가 만든다).
KB_COLLECTIONS = ("error_manual", "historical_case", "process_knowledge")
QDRANT_URL_ENV = "QDRANT_URL"
DEFAULT_QDRANT_URL = "http://localhost:6333"


def _preflight_data(fixtures: Optional[Path] = None) -> None:
    """실측 전 KB(Qdrant)·로그 DB(Postgres) 생존 확인.

    **2026-07-22 실측 — 이 함수가 없어서 하루를 잃었다.** 도커가 꺼진 채 라운드를 두 번
    돌렸는데, 실행은 끝까지 '성공'했다. Qdrant 가 없으면 cases·kb_hits·manual_hits·knob_map 이
    전부 빈 채로 가고, 카드 규칙 3(출처 2종 — spc/model/**kb**/**case**)을 못 채워
    evidence_cards 가 **세 도구 모두 0장**이 된다. 판정 정확도까지 함께 무너지지만
    로그에는 아무 에러도 안 남는다 — '측정 불가'가 '나쁜 성적'으로 기록된다.

    LLM preflight(_preflight)와 같은 취지다: **재료가 안 흐르면 아예 시작하지 않는다.**
    Qdrant 는 하드 스톱, Postgres 는 경고만 — 로그 근거가 없어도 파이프라인은 돌고
    근거가 얕아질 뿐이라 측정 자체가 무효가 되지는 않는다.
    """
    url = os.environ.get(QDRANT_URL_ENV, DEFAULT_QDRANT_URL).rstrip("/")
    try:
        with httpx.Client(timeout=PREFLIGHT_TIMEOUT_S) as client:
            resp = client.get(url + "/collections")
            resp.raise_for_status()
            got = {c["name"] for c in resp.json()["result"]["collections"]}
    except Exception as exc:
        raise SystemExit(
            f"Qdrant 응답 없음 ({type(exc).__name__}) — {url}\n"
            "  KB 가 없으면 근거 카드가 세 도구 모두 0장이 되고 판정도 함께 무너진다.\n"
            "  docker compose up -d postgres qdrant"
        ) from exc

    from src.agent_service.vectordb import physical_name  # noqa: PLC0415 — 무거운 의존성 지연 로딩

    missing = sorted({physical_name(c) for c in KB_COLLECTIONS} - got)
    if missing:
        raise SystemExit(
            f"Qdrant 컬렉션 누락: {missing}\n"
            f"  현재 있는 것: {sorted(got) or '없음'}\n"
            "  적재부터 해라 — KB 가 비면 '사례 없음'으로 조용히 진행된다."
        )
    logger.info("preflight OK — Qdrant %s (컬렉션 %d종)", url, len(KB_COLLECTIONS))

    try:
        from src.agent_service.app import db  # noqa: PLC0415

        with db.connect() as conn:
            conn.execute("SELECT 1")
        logger.info("preflight OK — Postgres")
    except Exception as exc:
        logger.warning(
            "Postgres 연결 실패 (%s) — 로그 근거 없이 진행한다(근거가 얕아짐). "
            "복구: docker compose up -d postgres",
            type(exc).__name__,
        )
        return

    _preflight_api_mode()
    _preflight_violation_seed(fixtures)


def _preflight_violation_seed(fixtures: Optional[Path]) -> None:
    """fixture alert_id 가 `spc_violations` 에 있는지 — recipe 축 생존 확인 (W9-0).

    **2026-08-05 실측 — 이 검사가 없어서 라운드 5회를 잃었다.** 운영에서는 B publisher 가
    알람 발행 **전에** `insert_violations()` 로 적재하고 우리가 그 행에서 `recipe_id` 를
    조회하는데, 재생(replay) 경로에는 그 적재가 없다. 조회가 0건이면 엔진이
    *"recipe_id=None · knob=C4 명목값 조회 불가"* 로 에스컬해 **recipe 옵션이 42/42 전부
    수치 없이 나간다**(round11~15). 로그에는 아무 에러도 없고, `_provisional` 도장도 안 찍힌다
    — 엔진이 정상적으로 "조회 불가"라고 답한 것이라 폴백이 아니기 때문이다.

    Qdrant 처럼 하드 스톱하지 않는 이유: 시딩 없이 재는 것이 **의도인 경우**(예: recipe 축을
    끈 대조군)가 있을 수 있다. 대신 **무엇이 죽는지**를 이름으로 말한다.
    """
    try:
        from sqlalchemy import text  # noqa: PLC0415

        from src.agent_service.app.db import sa_connect  # noqa: PLC0415
        from src.agent_service.fixtures.seed_violations import iter_fixture_alerts  # noqa: PLC0415

        ids = [a["alert_id"] for a in iter_fixture_alerts(fixtures)]
        if not ids:
            return
        with sa_connect() as conn:
            rows = conn.execute(
                text("SELECT DISTINCT alert_id FROM spc_violations WHERE alert_id = ANY(:ids)"),
                {"ids": ids},
            ).scalars().all()
    except Exception as exc:  # noqa: BLE001 — 검사 실패로 라운드를 막지 않는다
        logger.warning("시딩 검사 건너뜀 (%s: %s)", type(exc).__name__, exc)
        return

    covered, total = len(set(rows)), len(ids)
    _PROVENANCE["seeded"] = f"{covered}/{total}"
    if covered == total:
        logger.info("preflight OK — fixture 시딩 %d/%d (recipe 축 생존)", covered, total)
        return
    logger.warning(
        "⚠️ fixture 시딩 %d/%d — 미시딩분은 recipe_id 조회가 0건이라 **recipe 옵션이 수치 없이 "
        "에스컬**한다(측정 불가가 '나쁜 성적'으로 기록됨). 복구: "
        "python -m src.agent_service.fixtures.seed_violations",
        covered, total,
    )


#: 이번 실행의 재료 출처 — runs_history 에 함께 적힌다(append_run_history docstring 참조).
_PROVENANCE: dict[str, Any] = {}


def _fixture_stamp(fixtures: Path) -> dict[str, Any]:
    """fixture 세트의 **내용 지문** — 파일명이 아니라 내용을 센다 (2026-08-06, W40).

    `runs_history` 는 이미 `fixtures` 경로명을 남기지만, 경로는 **같은 이름 아래 내용이 바뀐 것**을
    못 잡는다. W9 에서 `alerts_v2` 를 만들며 여러 번 고칠 예정이라, 지금 지문을 안 박아두면
    *"round18 과 round21 이 같은 자로 잰 것인가"* 를 나중에 되물을 수 없다 — 8/5 에 태그
    (`round12-realengine`)를 믿었다가 다섯 라운드를 잘못 읽은 것과 **같은 종류의 사고**다.

    파일명까지 해시에 넣는 이유: 정답지가 **파일명**이라(`01_baseline_aging_*.json`) 이름만
    바뀌어도 채점이 달라진다.
    """
    import hashlib

    h = hashlib.sha256()
    files = sorted(fixtures.glob("*.json"))
    for p in files:
        h.update(p.name.encode("utf-8"))
        h.update(p.read_bytes())
    return {"fixtures": fixtures.name,
            "fixture_n": len(files),
            "fixture_hash": h.hexdigest()[:12]}


def _preflight_api_mode() -> None:
    """정량 엔진이 실물인지 확인 (`AGENT_API_MODE`) — 2026-08-05 신설.

    **mock 이면 `_compute_tuning` 이 DB 를 아예 보지 않고 `_stub_tuning` 을 쓴다.** 스텁은
    `parameter_id` 에 **위반 센서를 그대로** 넣는데(C15·C17 = 측정 센서), 그건 손잡이가 아니라서
    `enforce_knob_guard` 가 정확히 강등한다 → recipe 옵션이 전부 에스컬. 가드는 제 일을 한
    것이고, 재료가 가짜였을 뿐이다. round11~15 가 이 상태였다.

    하드 스톱하지 않는 이유: 스텁 대조군을 **의도적으로** 재는 경우가 있을 수 있다.
    대신 모드를 `runs_history` 에 남겨, 나중에 "그 라운드가 실물이었나"를 다시 묻지 않게 한다.
    """
    from src.agent_service.app.config import ApiMode, load_settings  # noqa: PLC0415

    mode = load_settings().api_mode
    _PROVENANCE["api_mode"] = mode.value if hasattr(mode, "value") else str(mode)
    if mode is ApiMode.REAL:
        logger.info("preflight OK — 정량 엔진 real (B5-4 실 엔진)")
        return
    logger.warning(
        "⚠️ AGENT_API_MODE=%s — 정량 엔진이 **스텁**이다. tuning.parameter_id 에 위반 센서가 "
        "그대로 들어가고(손잡이 아님) 손잡이 가드가 전부 강등해 **recipe 옵션이 수치 없이 "
        "에스컬**한다. 실 엔진으로 재려면 .env 의 AGENT_API_MODE=real (round11~15 가 이 상태였다)",
        _PROVENANCE["api_mode"],
    )


def build_backend(real: bool, fixtures: Optional[Path] = None):
    """Mock(기본) 또는 실 LLM. Mock 은 테스트 대본을 재사용해 **배관만** 검증한다.

    `fixtures` 는 시딩 커버리지 검사에만 쓴다(미지정이면 기본 디렉토리) — 이 함수는 `args` 를
    받지 않으므로 **호출자가 넘긴다.**
    """
    settings = load_settings()
    if real:
        logger.info("실 LLM 백엔드 (LLM_MODE=%s, url=%s)",
                    settings.llm_mode.value, settings.llm_base_url)
        _preflight(settings.llm_base_url)
        _preflight_data(fixtures)  # 재료가 안 흐르면 측정이 무효다 (2026-07-22·08-05)
        return make_backend(settings), settings

    from test_pipeline_fanout import MOCK_BY_TYPE, RoutingBackend
    from test_supervisor import SUP_JSON

    logger.info("MockBackend — 배관·채점 로직 검증용 (판정 품질 아님)")
    return RoutingBackend(MOCK_BY_TYPE, responses=[SUP_JSON] * 3), settings


# --- 컬럼 사전 (단일 소스) -------------------------------------------------------
# ⚠️ 이 dict 의 키 = 아래 evaluate_one 이 만드는 채점 레코드의 컬럼이다.
#    **컬럼을 추가/삭제하면 여기 설명도 같은 커밋에서 고칠 것** (그래야 뷰어 툴팁이 안 어긋난다).
#    이건 계약 스키마가 아니라 실험 계측용이라 자유롭게 바뀐다 — 대신 설명을 코드 옆에 붙여 자동 전달한다.
COLUMN_DOCS: dict[str, str] = {
    # ① 기본 (모든 행 — 입력)  ※ dict 순서 = 뷰어 컬럼 순서
    "alert_id": "[기본] 트리거 alert ID (ALERT-...)",
    "chamber_id": "[기본] 챔버 (SIM_CH_1~4)",
    "context_score": "[기본] B 게이트 점수 0~100 — <31 이면 gated(agent 미가동)",
    "is_gated": "[기본] 게이트 차단 여부. true=차단 → sup_/tool_ 필드가 아예 없음",
    "error": "[기본] 실행 오류 메시지 (있을 때만)",
    # ② 총괄(Supervisor) — sup_ (정답·판정 모두 sup단 평가라 여기 묶음)
    "sup_verdict_answer": "[총괄] 정답 verdict (fixture 파일명 유래·채점 기준지). edge 는 없음(null)",
    "sup_verdict_predict": "[총괄] LLM 이 낸 4지선다 판정(진단) — equipment_fault/process_shift/baseline_aging/escalate",
    "sup_selected_option": "[총괄] 고른 옵션(recipe/limit/manual_option) 또는 None(실행할 안). escalate·가드강등이면 None",
    "sup_confidence": "[총괄] 판정 확신도 0~1 (LLM 자기평가. fallback이면 0)",
    "sup_evidence_n": "[총괄] Brief 판정근거 '3줄'의 개수 — 계약상 반드시 3 (tool의 evidence_cards 와 다른 것)",
    "sup_evidence_empty_n": "[총괄] 근거 3줄 중 '해당 근거 없음' **표지**를 쓴 수(빈 근거). 실질 근거 = 3 − 이 값. ※ 정상 근거의 '급변 없음' 같은 표현은 세지 않음",
    "sup_masked_citations": "[총괄] 유령 인용 마스킹 수(가드③) = 잡힌 유령 개수. 0 이 정상",
    "sup_is_fallback": "[총괄] Brief 가 §6 fallback 인가(bool). true면 predict=escalate·conf=0·selected=None 이 코드로 강제된 실패 기본값 (LLM 판단 아님)",
    "sup_rejected_filled": "[총괄] 안 고른 옵션 중 rejected_because 채워진 개수 (정상=2)",
    # ⓘ 2026-07-22 파생 컬럼 2종 삭제 — selected 에 none_executable sentinel 이 생겨 불필요해졌다.
    #    · sup_selected_none_reason : (selected, verdict, is_fallback) 로 100% 파생됨(41/41 검증).
    #      원래 태어난 이유가 "selected=null 만으로는 정상/가드/누락을 구분 못 한다"였는데
    #      값 자체가 구분하게 되면서 전제가 사라졌다. reason 문자열 매칭 휴리스틱도 함께 제거.
    #    · sup_is_predict_selected_match : 가드②가 강제하므로 **항상 true**(실측 41/41) — 정보량 0.
    "sup_brief": "[총괄·중첩] reason·evidence(3줄)·counter_evidence·suspected_root_causes·wafer_disposition·rejected_because",
    # ③ tool(리포트 3종) — tool_
    "tool_reports_n": "[tool] 입력 리포트 수 (항상 3)",
    "tool_cards_n": "[tool] 근거카드 총 개수(3리포트 합). 0이면 카드 다 죽음 (sup_evidence_n 과 다른 것)",
    "tool_card_shapes": "[tool] 각 근거카드(evidence_cards)의 키 목록 (§5 형식 검증용)",
    "tool_fallbacks": "[tool] 3리포트 중 fallback(§6) 수. 0이 정상, >0 = tool LLM 파싱 실패",
    "tool_reports": (
        "[tool·중첩] 리포트 3종 요약 — 서술(option_type·confidence·rationale·uncertainty·"
        "escalate_reason·action·evidence_cards) + **옵션별 정량**: "
        "recipe=parameter_id·value_current·applied_value·value_proposed·delta_pct·hypothesis / "
        "limit=sensor_id·method·delta_sigma·shadow_eval·correction_id / "
        "manual=target_component·estimated_downtime_h. "
        "⚠️ 2026-08-05 이전 라운드에는 정량이 **없다** — 그때 값을 세면 '항상 None'이 나온다"
    ),
}


def write_column_docs() -> None:
    """COLUMN_DOCS 를 사이드카 JSON 으로 출력한다 — 뷰어가 읽어 헤더 툴팁을 띄운다.

    매 실행 호출하므로 컬럼을 바꾸면(위 COLUMN_DOCS 수정) 다음 실행에서 자동 반영된다.
    """
    COLUMNS_META.parent.mkdir(parents=True, exist_ok=True)
    COLUMNS_META.write_text(
        json.dumps(COLUMN_DOCS, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --- 1건 평가 --------------------------------------------------------------------
async def evaluate_one(alert, settings, backend, *, real: bool) -> dict[str, Any]:
    """alert 1건 → Brief → 채점 레코드. 실패해도 예외를 삼키고 기록만 남긴다(전체 중단 방지)."""
    stem = alert.alert_id
    expected = None  # 호출자가 채운다(파일명 기반)
    rec: dict[str, Any] = {"alert_id": alert.alert_id, "chamber_id": alert.chamber_id,
                           "context_score": alert.context_score}
    # 계측 수집 (W40) — 검색 doc_id·지연·토큰. **태스크별로 격리**된다(observe.py 참조).
    # 실패·게이트 경로에도 붙인다: "느려서 게이트에 걸린 것"도 관측 대상이다.
    from src.agent_service.app.observe import observing, summarize  # noqa: PLC0415

    with observing() as obs:
        try:
            # Mock 은 매번 새 백엔드가 필요하다(대본 소진) — real 은 재사용
            be = backend if real else build_backend(False)[0]
            reports, brief = await handle_alert_with_brief(
                alert, settings, backend=be,
                incident_id=f"INC-EVAL-{alert.alert_id.rsplit('-', 1)[-1]}",
            )
        except Exception as exc:  # noqa: BLE001 — 한 건 실패로 전체를 멈추지 않는다
            logger.warning("%s 평가 실패 — 기록만 (%s)", stem, exc)
            return {**rec, "error": str(exc), **summarize(obs)}

        if brief is None:
            return {**rec, "is_gated": True, **summarize(obs)}  # 게이트 미통과(B7) — 정상 경로
        rec = {**rec, **summarize(obs)}

    r = brief.supervisor_recommendation
    return {
        **rec,
        "is_gated": False,
        # --- 총괄(Supervisor Brief) 지표 — sup_ 접두 (정답은 caller 가 sup_verdict_answer 로 주입) ---
        "sup_verdict_predict": r.verdict,
        "sup_selected_option": r.selected,
        "sup_confidence": r.confidence,
        "sup_evidence_n": len(r.evidence),
        "sup_evidence_empty_n": sum(1 for e in r.evidence if _NO_EVIDENCE_MARK in e),
        "sup_masked_citations": sum(1 for e in r.evidence if "미확인 인용" in e),
        "sup_is_fallback": _FALLBACK_MARK in r.reason,
        "sup_rejected_filled": sum(
            1 for t, o in brief.parallel_options.items()
            if t != r.selected and o.rejected_because
        ),
        # --- tool(리포트 3종) 지표 — tool_ 접두 ---
        "tool_reports_n": len(reports),
        # evidence_cards 실패 형태 수집 (ADR-31 완화 설계용) — 리포트가 낸 카드의 **키 목록**만 남긴다.
        # 본문은 길고 개인차가 없으므로 형태만 보면 충분하다.
        "tool_card_shapes": [sorted(c.keys()) for r in reports
                             for c in (r.model_dump(mode="json").get("evidence_cards") or [])],
        "tool_cards_n": sum(len(r.model_dump(mode="json").get("evidence_cards") or []) for r in reports),
        "tool_fallbacks": sum(1 for r in reports if _FALLBACK_MARK in r.rationale),
        # ⚠️ **실제 서술을 남긴다.** 지표만 저장했다가 "근거가 설득력 있나"를 눈으로 볼 수
        # 없었다(2026-07-20). 판정 품질은 숫자로만 못 재고, 프롬프트를 고치려면 LLM 이
        # 무슨 문장을 썼는지 읽어야 한다. 재현이 비싸므로(EC2 재기동 + 7~10분) 반드시 보존.
        "sup_brief": {
            "reason": r.reason,
            "evidence": list(r.evidence),
            "counter_evidence": r.counter_evidence,
            "suspected_root_causes": list(brief.suspected_root_causes),
            "wafer_disposition": brief.wafer_disposition.model_dump(mode="json"),
            "rejected_because": {
                t: o.rejected_because for t, o in brief.parallel_options.items()
            },
        },
        "tool_reports": [_summarize_tool_report(rp) for rp in reports],
    }


#: 옵션별 **정량 필드** — 이게 없으면 "수치 제안이 실제로 나갔나"를 잴 수 없다 (2026-08-05 신설).
#  하루 종일 recipe 축을 논하다 지표 자체가 없다는 것을 뒤늦게 알았다: 요약이 7개 필드뿐이라
#  `parameter_id` 를 `.get()` 으로 읽으면 **없는 키라 항상 None** 이고, 그걸 "제안 0건"으로
#  오독했다. 값이 안 실리는 것보다 **없는 값을 0으로 읽는 것**이 더 위험하다.
_QUANT_BY_OPTION: dict[str, tuple[str, ...]] = {
    "recipe_option": ("parameter_id", "value_current", "applied_value",
                      "value_proposed", "delta_pct", "hypothesis"),
    "limit_option": ("sensor_id", "method", "delta_sigma", "shadow_eval", "correction_id"),
    "manual_option": ("target_component", "estimated_downtime_h"),
}


def _summarize_tool_report(rp) -> dict[str, Any]:
    """리포트 → 결과 jsonl 에 남길 요약. 서술 + **옵션별 정량 필드**.

    전문을 싣지 않는 이유는 기존과 같다(payload 비대). 다만 정량은 **한 줄이라도 남긴다** —
    `_QUANT_BY_OPTION` 주석 참조. 옵션에 없는 필드는 키 자체를 넣지 않는다(스키마가 다르므로).
    """
    dumped = rp.model_dump(mode="json")
    out: dict[str, Any] = {
        "option_type": rp.option_type,
        "confidence": rp.confidence,
        "rationale": rp.rationale,
        "uncertainty": rp.uncertainty,
        "escalate_reason": rp.escalate_reason,
        # manual 전용 — ③-C(변별 신호 없이 정비 제안) 분모 판정용. 다른 옵션엔 없다.
        "action": getattr(rp, "action", None),
        "evidence_cards": dumped.get("evidence_cards"),
    }
    for key in _QUANT_BY_OPTION.get(rp.option_type, ()):
        if key in dumped:
            out[key] = dumped[key]
    return out


# --- 채점 ------------------------------------------------------------------------
def score(records: list[dict[str, Any]]) -> dict[str, Any]:
    """혼동행렬 + 지표 출력 후 요약 dict 반환. 정답이 있는 건만 정확도에 포함."""
    scored = [r for r in records if r.get("sup_verdict_answer") and not r.get("is_gated") and "sup_verdict_predict" in r]
    gated = [r for r in records if r.get("is_gated")]
    edge = [r for r in records if not r.get("sup_verdict_answer") and not r.get("is_gated") and "sup_verdict_predict" in r]
    errors = [r for r in records if r.get("error")]

    # 요약(추이 기록)용 기본값 — scored/live 가 비어도 안전하게 반환하도록 미리 초기화
    hit = 0
    by_verdict: dict = defaultdict(lambda: [0, 0])
    fb = tool_fb = cards = 0
    conf: list = []

    print("\n" + "=" * 72)
    print(f"평가 대상 {len(records)}건 — 채점 {len(scored)} · 엣지 {len(edge)} · "
          f"게이트차단 {len(gated)} · 오류 {len(errors)}")
    print("=" * 72)

    if scored:
        hit = sum(1 for r in scored if r["sup_verdict_predict"] == r["sup_verdict_answer"])
        print(f"\n▶ 판정 정확도: {hit}/{len(scored)} = {hit / len(scored) * 100:.1f}%")

        # 4×4 혼동행렬 — 무엇을 무엇으로 착각하는지가 프롬프트 수정 지점
        matrix: dict[str, Counter] = defaultdict(Counter)
        for r in scored:
            matrix[r["sup_verdict_answer"]][r["sup_verdict_predict"]] += 1
        w = max(len(v) for v in VERDICTS) + 2
        print(f"\n  혼동행렬 (행=기대, 열=실제)")
        print("  " + " " * w + "".join(f"{v[:12]:>14}" for v in VERDICTS))
        for exp in VERDICTS:
            row = "".join(f"{matrix[exp][act] or '·':>14}" for act in VERDICTS)
            print(f"  {exp:<{w}}{row}")

        by_verdict = defaultdict(lambda: [0, 0])
        for r in scored:
            by_verdict[r["sup_verdict_answer"]][1] += 1
            if r["sup_verdict_predict"] == r["sup_verdict_answer"]:
                by_verdict[r["sup_verdict_answer"]][0] += 1
        print("\n  판정별 정확도")
        for v in VERDICTS:
            h, n = by_verdict[v]
            if n:
                print(f"    {v:<18} {h}/{n}  ({h / n * 100:.0f}%)")

    live = [r for r in records if "sup_verdict_predict" in r]
    if live:
        fb = sum(1 for r in live if r["sup_is_fallback"])
        bad_n = sum(1 for r in live if r["sup_evidence_n"] != 3)
        masked = sum(r["sup_masked_citations"] for r in live)
        no_ev = sum(r["sup_evidence_empty_n"] for r in live)
        # 가드②가 강제하는 정합 대신, **실제로 흔들리는 값**을 본다:
        #   selected=null = LLM 이 칸을 건너뜀(정상 Brief 는 null 이 아니다 — sentinel 도입 후)
        sel_null = sum(1 for r in live if r["sup_selected_option"] is None)
        sel_none = sum(1 for r in live if r["sup_selected_option"] == NONE_EXECUTABLE)
        conf = [r["sup_confidence"] for r in live]
        tool_fb = sum(r.get("tool_fallbacks", 0) for r in live)   # 형식 회귀 사각지대 메움
        cards = sum(r.get("tool_cards_n", 0) for r in live)

        print(f"\n▶ 총괄(Brief) 품질 지표 ({len(live)}건)")
        print(f"    스키마 통과      : {len(live) - fb}/{len(live)}  "
              f"(fallback {fb}건 = {fb / len(live) * 100:.1f}%)  ← ADR-31 드랍률 >10% 면 완화 검토")
        print(f"    evidence 3개 위반: {bad_n}건")
        print(f"    '근거 없음' 표기 : {no_ev}건  (근거 부족을 정직히 표시한 횟수)")
        print(f"    유령 인용 마스킹 : {masked}건")
        print(f"    selected 없음    : {sel_none}건(none_executable — 정상) · "
              f"**{sel_null}건 누락(null — LLM 이 건너뜀)**")
        print(f"    confidence       : 평균 {sum(conf) / len(conf):.2f} · "
              f"최소 {min(conf):.2f} · 최대 {max(conf):.2f}")
        print(f"\n▶ tool(리포트 3종) 품질 지표  ← 형식 회귀 감시")
        print(f"    tool fallback    : {tool_fb}건  (0이 정상 · >0 = tool LLM 죽음)")
        print(f"    근거카드 생성    : {cards}장  (0이면 카드 다 죽음)")

    if errors:
        print(f"\n▶ 오류 {len(errors)}건")
        for r in errors[:5]:
            print(f"    {r['alert_id']}: {r['error'][:70]}")
    print()

    # 추이 기록용 요약 (main_async 가 runs_history 에 누적)
    summary: dict[str, Any] = {
        "accuracy_pct": round(hit / len(scored) * 100, 1) if scored else None,
        "n_scored": len(scored),
    }
    for v in VERDICTS:
        h, n = by_verdict[v]
        summary[f"acc_{v}"] = f"{h}/{n}" if n else "-"
    summary["fallback"] = fb
    summary["tool_fallbacks"] = tool_fb
    summary["cards_n"] = cards
    summary["conf_mean"] = round(sum(conf) / len(conf), 2) if conf else None
    summary["n_gated"] = len(gated)
    return summary


# --- 읽기용 덤프 --------------------------------------------------------------------
# 지표(숫자)만으로는 "근거가 설득력 있나"를 판단할 수 없다. 프롬프트를 고치려면
# LLM 이 실제로 쓴 문장을 읽어야 한다 — 그 용도의 출력.
def dump_readable(records: list[dict[str, Any]], only_wrong: bool = False) -> None:
    """저장된 결과를 사람이 읽는 형태로 출력한다 (--read)."""
    live = [r for r in records if r.get("sup_brief")]
    if only_wrong:
        live = [r for r in live if r.get("sup_verdict_answer") and r["sup_verdict_predict"] != r["sup_verdict_answer"]]
        print(f"\n\n※ 오판정 {len(live)}건만 표시\n")
    if not live:
        print("표시할 결과가 없다 — 이전 실행이 서술을 저장하지 않았거나(구버전) 대상이 없다.")
        return
    for r in live:
        b = r["sup_brief"]
        hit = "OK " if r.get("sup_verdict_predict") == r.get("sup_verdict_answer") else "XX "
        print("=" * 78)
        print(f"{hit}{r['alert_id']}  ({r['chamber_id']}, score {r['context_score']})")
        print(f"   기대={r.get('sup_verdict_answer') or '-'}  실제={r['sup_verdict_predict']}  "
              f"selected={r['sup_selected_option']}  conf={r['sup_confidence']}")
        print(f"\n[판정 사유]\n  {b['reason']}")
        print("\n[근거 3]")
        for e in b["evidence"]:
            print(f"  - {e}")
        print(f"\n[반증]\n  {b['counter_evidence']}")
        if b.get("suspected_root_causes"):
            print("\n[원인 후보]")
            for c in b["suspected_root_causes"]:
                print(f"  - {c}")
        print("\n[기각 사유]")
        for t, why in (b.get("rejected_because") or {}).items():
            if t != r["sup_selected_option"]:
                print(f"  {t}: {why}")
        wd = b.get("wafer_disposition") or {}
        print(f"\n[wafer 처분] {wd.get('recommendation')} - {wd.get('reason')}")
        print("\n[tool 리포트]")
        for rp in r.get("tool_reports", []):
            cards = len(rp.get("evidence_cards") or [])
            print(f"  - {rp['option_type']} (conf {rp['confidence']}, 카드 {cards}장)")
            print(f"      {rp['rationale'][:150]}")
            if rp.get("escalate_reason"):
                print(f"      ! {rp['escalate_reason'][:120]}")
        print()


# --- 실행 ------------------------------------------------------------------------
def load_done(path: Path) -> set[str]:
    """--resume 용 — 이미 평가한 alert_id (증분 저장의 요점)."""
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            done.add(json.loads(line)["alert_id"])
        except Exception:  # noqa: BLE001 — 깨진 줄은 무시
            continue
    return done


def _gate_open(path: Path) -> bool:
    """이 fixture 가 context_score 게이트(B7=31)를 통과하는지 — 샘플링 우선순위용.

    fixture 는 판정별로 below_gate~critical 6단계인데, 앞에서부터 뽑으면 전부 below_gate 라
    Supervisor 가 아예 안 돌아 채점이 0건이 된다(실측). 판정 품질을 보려면 게이트 통과분이어야.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("context_score", 0) >= 31
    except Exception:  # noqa: BLE001 — 파손 fixture
        return False


def select_targets(args, fixtures: Optional[Path] = None) -> list[tuple[Path, Optional[str]]]:
    """평가 대상 fixture 선별 — 부분 실행 옵션 적용. fixtures 미지정 시 `--fixtures`/기본에서 해석."""
    fixtures = fixtures or fixtures_dir(args)
    pairs = [(p, expected_verdict(p.stem)) for p in sorted(fixtures.glob("*.json"))]
    if args.only:
        pairs = [(p, e) for p, e in pairs if e == args.only]
    if args.gate_open_only:
        pairs = [(p, e) for p, e in pairs if _gate_open(p)]
    if args.sample_per_verdict:
        # 게이트 통과분을 **먼저** 고른다 — 판정 품질을 보려면 Supervisor 가 돌아야 하므로.
        picked, seen = [], Counter()
        for p, e in sorted(pairs, key=lambda pe: not _gate_open(pe[0])):
            key = e or "edge"
            if seen[key] < args.sample_per_verdict:
                picked.append((p, e))
                seen[key] += 1
        pairs = sorted(picked, key=lambda pe: pe[0].name)
    if args.limit:
        pairs = pairs[: args.limit]
    return pairs


async def main_async(args) -> int:
    run_results = results_path(args.tag)  # --tag 지정 시 라운드별 파일
    if args.read:  # 저장본 읽기 — LLM 을 부르지 않는다
        if not run_results.exists():
            logger.error("결과 파일 없음: %s (먼저 평가를 돌릴 것)", run_results)
            return 1
        write_column_docs()  # 뷰어 툴팁 사이드카 갱신
        recs = [json.loads(l) for l in run_results.read_text(encoding="utf-8").splitlines() if l.strip()]
        dump_readable(recs, only_wrong=args.only_wrong)
        return 0

    fixtures = fixtures_dir(args)          # 기본 fixtures/alerts · --fixtures 로 교체 (W37)
    # ablation 은 **백엔드 생성보다 먼저** 잡아야 한다 — tool 프롬프트가 만들어지기 전에 정해져야
    # "예시를 뺀 프롬프트"가 실제로 나간다. 오타는 여기서 즉시 죽는다(set_ablation 이 raise).
    from src.agent_service.app.observe import ablation_tag, set_ablation  # noqa: PLC0415

    set_ablation(args.ablate or [])
    _PROVENANCE.update(_fixture_stamp(fixtures))   # 내용 지문 (경로명만으론 못 잡는다)
    _PROVENANCE["ablate"] = ablation_tag()
    # 동시성 — **지연 수치의 자(尺)다** (2026-08-10 신설). `lat_*` 는 동시성을 빼고 읽으면
    # 비교가 성립하지 않는다: 실측 round25(기본) 중위 28.7s·초과 26.2% ↔ round25-conc1
    # 중위 23.1s·초과 11.9%. 그런데 그 구분이 **태그 문자열에만** 있었다 — 이 파일 상단
    # `append_run_history` docstring 이 `api_mode` 를 두고 적어 둔 원칙("태그는 사람이 붙이는
    # 이름이라 틀릴 수 있다 — 모드는 코드가 적는다")이 이 축에는 적용돼 있지 않았다.
    # PM-3(`agent.incident_max_parallel`) 상한을 이 이력에서 역산할 것이므로 반드시 남긴다.
    _PROVENANCE["concurrency"] = int(args.concurrency)
    backend, settings = build_backend(args.real, fixtures)   # 시딩 검사가 이 경로를 본다
    targets = select_targets(args, fixtures)
    run_results.parent.mkdir(parents=True, exist_ok=True)
    write_column_docs()  # 뷰어 툴팁 사이드카 (컬럼 설명 단일 소스 → 자동 전달)

    done = load_done(run_results) if args.resume else set()
    if not args.resume and run_results.exists():
        run_results.unlink()  # 새 실행 — 이전 결과 초기화

    alerts_by_file = {}
    for alert in iter_replay_alerts(fixtures):  # 파손 1건은 여기서 스킵됨(6-2)
        alerts_by_file[alert.alert_id] = alert

    # 파일 ↔ alert 매칭 (파일명이 정답지라 파일 순서를 따른다)
    work: list[tuple[Any, Optional[str]]] = []
    for path, exp in targets:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # 파손 fixture
            logger.info("%s 파싱 불가 — 스킵 (의도된 파손 케이스)", path.name)
            continue
        alert = alerts_by_file.get(data.get("alert_id"))
        if alert is None:
            continue
        if alert.alert_id in done:
            continue
        work.append((alert, exp))

    logger.info("평가 대상 %d건 (resume 스킵 %d건) · 백엔드=%s",
                len(work), len(done), "real" if args.real else "mock")

    sem = asyncio.Semaphore(args.concurrency)
    records: list[dict[str, Any]] = []

    async def one(alert, exp):
        async with sem:  # 엔드포인트에 몰아치지 않게
            rec = await evaluate_one(alert, settings, backend, real=args.real)
            rec["sup_verdict_answer"] = exp
            records.append(rec)
            # 증분 저장 — 중간에 죽어도 앞부분은 살아 있다
            with run_results.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            mark = "·" if rec.get("is_gated") else ("✔" if rec.get("sup_verdict_predict") == exp else "✗")
            logger.info("  %s %s → %s (기대 %s)", mark, alert.alert_id,
                        rec.get("sup_verdict_predict", "gated"), exp or "—")

    await asyncio.gather(*(one(a, e) for a, e in work))

    if args.resume and done:  # 이전 결과도 합쳐서 채점
        prev = [json.loads(l) for l in run_results.read_text(encoding="utf-8").splitlines()
                if l.strip()]
        records = prev
    summary = score(records)
    # ②③④·지연·토큰까지 **요약에 함께** 남긴다 (2026-08-06, W66).
    # 그전에는 ①(정확도·판정별)만 남아서, 나머지 축의 추이를 볼 때마다 일회용 스크립트로
    # 다시 계산해야 했다 — 손으로 만든 추이표는 재현되지 않는다.
    try:
        from src.agent_service.analyze_quality import round_metrics  # noqa: PLC0415

        summary = {**summary, **round_metrics(records)}
    except Exception as exc:  # noqa: BLE001 — 요약 실패가 라운드 결과를 버리게 두지 않는다
        logger.warning("확장 지표 계산 실패 — ①만 기록한다 (%s: %s)", type(exc).__name__, exc)
    if args.real:  # 실측만 추이에 기록(mock 은 판정 품질 아님)
        append_run_history(args.tag, summary, real=args.real, provenance=dict(_PROVENANCE))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Supervisor 판정 평가 하네스")
    ap.add_argument("--real", action="store_true", help="실 LLM 사용 (EC2 필요, 7~10분)")
    ap.add_argument("--only", choices=VERDICTS, help="특정 판정만 평가")
    ap.add_argument("--limit", type=int, help="앞에서 N건만")
    ap.add_argument("--sample-per-verdict", type=int, metavar="N",
                    help="판정별 N건만 (튜닝 루프용 — 1이면 4건)")
    ap.add_argument("--gate-open-only", action="store_true",
                    help="게이트 통과(score>=31) fixture 만 — 판정 품질 측정용")
    ap.add_argument("--read", action="store_true",
                    help="저장된 결과의 **실제 서술**을 읽기 좋게 출력 (LLM 재호출 없음)")
    ap.add_argument("--only-wrong", action="store_true",
                    help="--read 와 함께: 오판정 건만 표시 (프롬프트 튜닝용)")
    ap.add_argument("--fixtures", type=str, default=None, metavar="DIR",
                    help="fixture 디렉토리 교체 (기본: fixtures/alerts). W9 대조용 — "
                         "예: --fixtures src/agent_service/fixtures/alerts_v2")
    ap.add_argument("--resume", action="store_true", help="이전 결과 이어서")
    ap.add_argument("--tag", type=str, default=None,
                    help="라운드 라벨 — 결과를 supervisor_results_<tag>.jsonl 로 저장 + runs_history 추이 기록 (예: round3)")
    ap.add_argument("--concurrency", type=int, default=3, help="동시 실행 수 (기본 3)")
    ap.add_argument("--ablate", action="append", choices=["kb", "fewshot", "shap"], metavar="재료",
                    help="이 재료를 **빼고** 돈다 (여러 번 지정 가능). 우리가 만든 것이 실제로 "
                         "기여하는지 재는 용도 — 기준선 라운드와 같은 fixture 로 대조할 것. "
                         "예: --ablate kb --ablate fewshot")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
