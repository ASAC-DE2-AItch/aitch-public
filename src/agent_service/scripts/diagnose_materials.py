# -*- coding: utf-8 -*-
"""재료 진단 — 라운드가 쓸 **입력이 실제로 흐르는지**를 fixture 전수로 확인한다 (LLM 미사용).

**왜 만들었나 (2026-08-05).** 하루에 두 번, *보이는 것*으로 *안 보이는 것*을 짐작하다 틀렸다.
  · "recipe 제안 0/42" → 결과 파일에 **없는 필드**를 읽고 0으로 읽었다
  · "limit 은 대부분 스텁" → 경고 두 줄을 보고 비율을 짐작했다. 실제는 스텁 10%
둘 다 **재료를 직접 세지 않아서** 생겼다. 이 스크립트는 짐작할 자리를 없앤다.

이 도구가 답하는 것 — *"각 단계에서 몇 건이 실제로 값을 받았나"*:
    alert 파싱 · 예측 조인 · recipe_id 해석(시딩) · 튜닝 제안 · applied_value ·
    limit recalc 조달 · KB(kb_hits·cases·manual) · knob_map · 페어링 로그

**LLM 을 부르지 않는다** — EC2 없이 언제든 돌릴 수 있어야 라운드 전에 습관적으로 보게 된다.
비어 있어도 죽지 않는다: 빈 것을 **세는 게** 목적이라 예외는 그 항목의 실패로 기록만 한다.

사용:
    python -m src.agent_service.scripts.diagnose_materials
    python -m src.agent_service.scripts.diagnose_materials --fixtures src/agent_service/fixtures/alerts_v2
    python -m src.agent_service.scripts.diagnose_materials --verbose   # 결측 fixture 이름까지
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT.parents[1]))

from src.agent_service.app.schemas.alert import AlertModel  # noqa: E402

logger = logging.getLogger("diagnose")

#: 단계별 판정 — (라벨, 값이 "있다"고 볼 조건). 비면 그 단계에서 재료가 끊긴 것이다.
_STAGES = (
    "예측 조인(predicted_c65)",
    "SHAP top3",
    "recipe_id 해석(시딩)",
    "tuning 산출",
    "  └ 수치 제안(parameter_id)",
    "  └ applied_value",
    "limit recalc 조달",
    "  └ shadow_eval",
    "KB kb_hits",
    "KB cases",
    "KB manual_hits",
    "knob_map",
    "페어링 로그(recipe_logs)",
)


def _iter_alerts(fixtures: Path):
    """fixture 알람 순회 — 파손분은 세고 넘어간다(그것도 정보다)."""
    broken = []
    for p in sorted(fixtures.glob("*.json")):
        try:
            alert = AlertModel.model_validate(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 — 파손 fixture 는 의도된 재료
            broken.append((p.name, type(exc).__name__))
            continue
        yield p.name, alert
    if broken:
        print(f"\n  ※ 파싱 실패 {len(broken)}건 (의도된 파손 fixture 포함): "
              + ", ".join(f"{n}({e})" for n, e in broken))


def diagnose(fixtures: Path, verbose: bool = False) -> int:
    """단계별 커버리지를 표로 낸다. 반환값은 프로세스 exit code(항상 0 — 관측 도구)."""
    from src.agent_service.app import pipeline as P

    have: Counter = Counter()
    missing: dict[str, list[str]] = defaultdict(list)
    total = 0
    knob_map = P._load_knob_map()
    knob_n = len(knob_map or [])

    for name, alert in _iter_alerts(fixtures):
        total += 1
        inc = f"INC-DIAG-{alert.alert_id.rsplit('-', 1)[-1]}"

        def mark(stage: str, ok: bool) -> None:
            if ok:
                have[stage] += 1
            else:
                missing[stage].append(name)

        pc = alert.prediction_context
        mark(_STAGES[0], bool(pc and pc.predicted_c65 is not None))
        mark(_STAGES[1], bool(pc and pc.shap_top3))

        # recipe 계열 — 조달 실패는 그 단계의 결측으로 센다(예외로 죽지 않는다)
        try:
            tuning = P._compute_tuning(alert, inc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tuning 실패 %s: %s", name, exc)
            tuning = None
        mark(_STAGES[3], tuning is not None)
        mark(_STAGES[4], bool(tuning and tuning.get("parameter_id")))
        mark(_STAGES[5], bool(tuning and tuning.get("applied_value") is not None))

        try:
            from src.agent_b_spc.recipe_writer import resolve_alert_context

            from src.agent_service.app.db import sa_connect
            with sa_connect() as conn:
                rid, _ = resolve_alert_context(conn, alert.alert_id)
        except Exception:  # noqa: BLE001
            rid = None
        mark(_STAGES[2], rid is not None)

        try:
            recalc = P._fetch_recalc(alert)
        except Exception:  # noqa: BLE001
            recalc = None
        mark(_STAGES[6], recalc is not None)
        mark(_STAGES[7], bool(recalc and recalc.get("shadow_eval")))

        try:
            ev = P._search_recipe_evidence(alert)
        except Exception:  # noqa: BLE001
            ev = {}
        mark(_STAGES[8], bool(ev.get("kb_hits")))
        mark(_STAGES[9], bool(ev.get("cases")))

        try:
            mv = P._search_manual_evidence(alert)
        except Exception:  # noqa: BLE001
            mv = {}
        mark(_STAGES[10], bool(mv.get("manual_hits")))

        mark(_STAGES[11], knob_n > 0)

        try:
            pl = P._fetch_pairing_logs(ev.get("cases"))
        except Exception:  # noqa: BLE001
            pl = {}
        mark(_STAGES[12], bool(pl.get("recipe_logs")))

    if not total:
        print("fixture 0건 — 경로를 확인해라"); return 0

    print(f"\n{'단계':30}{'있음':>8}{'비율':>8}   판단")
    print("-" * 72)
    for st in _STAGES:
        n = have[st]
        pct = n / total * 100
        # 0% 는 "구조적으로 없음"일 수도 있어 단정하지 않는다 — 확인 대상으로만 표시한다.
        flag = "🔴 전무 — 확인" if n == 0 else ("🟡 일부" if n < total else "✅")
        print(f"{st:30}{n:>4}/{total:<3}{pct:>7.0f}%   {flag}")

    if verbose:
        print("\n결측 상세 (앞 6건씩)")
        for st in _STAGES:
            if missing[st]:
                print(f"  {st}: " + ", ".join(missing[st][:6])
                      + (f" … 외 {len(missing[st]) - 6}건" if len(missing[st]) > 6 else ""))

    print("\n※ 0% 가 곧 고장은 아니다 — 구조적으로 없는 것도 있다(예: 첫 스텝이면 applied_value 는 없다).")
    print("  판단은 '왜 없는지'를 아는 사람이 한다. 이 표는 **짐작을 없애는 것**까지가 역할이다.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="라운드 재료 진단 (LLM 미사용)")
    ap.add_argument("--fixtures", type=str, default=None, metavar="DIR")
    ap.add_argument("--verbose", action="store_true", help="결측 fixture 이름까지")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    d = Path(args.fixtures) if args.fixtures else SERVICE_ROOT / "fixtures" / "alerts"
    if not d.is_dir():
        raise SystemExit(f"fixture 디렉토리가 아니다: {d}")
    print(f"재료 진단 — {d}")
    return diagnose(d, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
