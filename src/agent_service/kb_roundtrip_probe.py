# -*- coding: utf-8 -*-
"""KB 왕복 프로브 — `hf_cache` 채널이 실제로 건너가는지 **한 번 태워본다** (인프라 스텝 4 · #144).

왜 있나:
  `hf_cache` 볼륨(bge-m3 임베딩)이 비어 있거나 엉뚱한 볼륨이 물리면, 임베딩 로드가 실패하고
  파이프라인은 **무중단으로 근거 0장 리포트**를 만든다 (헌법 6-3 폴백이 의도대로 동작한 결과다).
  프로세스 정상 · 로그 WARNING 한 줄 · 화면에는 Brief 가 뜬다 — **밖에서는 성공과 구별되지 않는다.**

  2026-08-10 실측으로 그 함정이 실재함을 확인했다: `hf_cache` 볼륨이 **두 개**였고
  (`aitch_hf_cache` 6.4GB / `aitch-agent_service_hf_cache` 4KB 빈 것) compose 가
  `name:` 으로 못박아 둔 덕에 실물이 물렸다. 못박기가 없었으면 조용히 빈 쪽이 물렸다.

  런타임 계측은 이미 있다 — `pipeline._note_kb_empty()` 가 컬렉션별 0장을 세고 WARNING 을
  남긴다. **없던 것은 "0장이면 실패로 판정하는 게이트"** 다. 계측은 사후에 읽는 것이고,
  게이트는 **시작 전에 막는 것**이다 (#130 회신2 에서 PM 이 찬성한 항목).

무엇을 태우나:
  운영 파이프라인과 **같은 함수**(`pipeline._search_recipe_evidence`)를 fixture alert 로 부른다.
  같은 임베딩 로드 · 같은 Qdrant 질의 경로다 — 여기서 통과하면 운영 경로도 통과한다.
  ⚠️ **LLM 은 필요 없다.** KB 검색은 LLM 호출 **전에** 끝난다 (실측 2026-08-10: vLLM 이
  400 을 돌려준 폴백 Brief 도 `evidence` 3건을 그대로 달고 나왔다).

어디서 도나:
  컨테이너 안에서 도는 것이 정답이다 — 호스트 백그라운드에서는 WDAC 가 torch `_C` DLL 을
  막아 **에러 없이 0장**이 된다 (그러면 이 프로브가 잡으려던 것과 똑같은 거짓 신호가 된다).

      docker exec fdc-agent-service python -m src.agent_service.kb_roundtrip_probe

  ⚠️ **`scripts/` 가 아니라 패키지 최상위에 둔 이유** — `.dockerignore` 가
  `src/agent_service/scripts/` 를 제외해서 이미지에 안 들어간다. 컨테이너 안에서 돌아야 하는
  도구라 `analyze_quality.py`·`eval_supervisor.py` 와 같은 자리에 둔다.

종료 코드: 0 = 왕복 성공 / 1 = 근거 0장(채널 끊김) / 2 = 재료·환경 문제로 판정 불가
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# 컨테이너( /app/src/agent_service/ )와 레포( <root>/src/agent_service/ ) 양쪽에서 돌아야 한다.
_SRC = Path(__file__).resolve().parents[1]          # .../src
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("kb_probe")

#: 프로브에 쓸 fixture — 판정 내용은 상관없다. **검색어가 만들어질 만큼 위반이 실려 있으면** 된다.
_PROBE_FIXTURE = "01_baseline_aging_below_gate.json"


def main() -> int:
    """fixture 1건으로 KB 검색을 태우고 0장이면 실패로 판정한다."""
    from agent_service.app.config import FIXTURES_DIR
    from agent_service.app.schemas.alert import AlertModel

    path = Path(FIXTURES_DIR) / _PROBE_FIXTURE
    if not path.exists():
        cands = sorted(Path(FIXTURES_DIR).glob("*.json"))
        if not cands:
            print(f"[kb-probe] FAIL(2) fixture 없음 — {FIXTURES_DIR}")
            return 2
        path = cands[0]

    try:
        alert = AlertModel.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001 — 재료 문제는 채널 판정과 구분한다
        print(f"[kb-probe] FAIL(2) fixture 파싱 실패 — {path.name}: {exc}")
        return 2

    # ⚠️ pipeline 을 **여기서** import 한다 — 위에서 하면 임베딩 로드 실패가 import 시점에
    #    터져 "fixture 문제"와 "채널 문제"가 같은 자리에서 죽는다.
    from agent_service.app import pipeline as P

    P.reset_kb_empty_counts()
    got = P._search_recipe_evidence(alert) or {}
    kb, cases = got.get("kb_hits") or [], got.get("cases") or []
    empty = P.kb_empty_counts()

    print(f"[kb-probe] fixture={path.name}  kb_hits={len(kb)}  cases={len(cases)}")
    if empty:
        print(f"[kb-probe] 0장 카운터: {empty}")

    if not kb and not cases:
        print("[kb-probe] FAIL(1) 근거 0장 — hf_cache(임베딩) 또는 Qdrant 채널이 끊겼다.")
        print("  확인 순서:")
        print("    1) 볼륨 실물   docker run --rm -v aitch_hf_cache:/m alpine du -sh /m   # 수 GB 여야 한다")
        print("    2) 마운트      docker inspect fdc-agent-service --format '{{range .Mounts}}{{.Name}} {{end}}'")
        print("    3) Qdrant      curl -s http://localhost:6333/collections")
        return 1

    # 한쪽만 비는 것은 '채널 끊김'이 아니라 '그 질의에 맞는 문서가 없음'일 수 있다 —
    # 채널 판정(exit 1)과 섞지 않고 경고로만 남긴다.
    if not kb or not cases:
        print("[kb-probe] WARN 한쪽 컬렉션이 0장 — 채널은 살아 있으나 질의·적재를 확인할 것")

    print("[kb-probe] PASS 왕복 성공")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
