# -*- coding: utf-8 -*-
"""관통 데모 — PM mock_alert_publisher 데이터를 우리 파이프라인에 먹여 리포트 값 출력.

C4-1/C5 개발 중 "지금 리포트가 어디까지 채워졌나"를 눈으로 확인하는 개발용 도구.
(테스트 아님 — pytest 수집 대상 아니고, 값을 사람이 읽으려는 용도.)

흐름: PM MockAlertFactory(계약 §3 발행기) → AlertModel 파싱 → handle_alert
      (게이트 + 3조수 팬아웃) → 각 리포트를 JSON 으로 출력.

사용법 (src/ 디렉토리에서 — run.py 와 동일):
    cd src
    PYTHONIOENCODING=utf-8 python -m agent_service.script.demo_pipeline
    PYTHONIOENCODING=utf-8 python -m agent_service.script.demo_pipeline --mode spike --count 4
    PYTHONIOENCODING=utf-8 python -m agent_service.script.demo_pipeline --mode storm --count 6

모드: mix(기본) / drift / spike / storm / multi / quiet — PM 발행기 정의와 동일.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 이 파일: src/agent_service/script/demo_pipeline.py → parents[3] = repo 루트
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))          # src.simulator.* 임포트용 (PM 발행기)
sys.path.insert(0, str(REPO_ROOT / "src"))  # agent_service.* 임포트용

from src.simulator.mock_alert_publisher import MockAlertFactory  # noqa: E402
from src.simulator.message_builder import load_params  # noqa: E402

from agent_service.app.config import load_settings  # noqa: E402
from agent_service.app.pipeline import gate_open, handle_alert  # noqa: E402
from agent_service.app.schemas.alert import AlertModel  # noqa: E402


def run(mode: str, count: int, seed: int) -> None:
    """PM 발행기로 count건 생성 → 파이프라인 통과 → 리포트 값 출력."""
    settings = load_settings()
    factory = MockAlertFactory(load_params(), seed=seed)
    pairs = factory.generate(mode, count)  # [(kind, alert_dict), ...]

    for i, (kind, raw) in enumerate(pairs, 1):
        alert = AlertModel.model_validate(raw)  # 우리 수신 스키마로 파싱
        reports = asyncio.run(handle_alert(alert, settings))

        print("\n" + "=" * 72)
        print(f"[{i}] PM 발행 alert  ·  종류={kind}")
        print(f"    alert_id      : {alert.alert_id}")
        print(f"    chamber       : {alert.chamber_id}")
        print(f"    context_score : {alert.context_score}   (게이트 B7=31)")
        for v in alert.violations:
            print(f"    violation     : {v.sensor} {v.rule_id} {v.severity} ({v.window})")
        print(f"    predicted_c65 : {alert.prediction_context.predicted_c65}")
        open_ = gate_open(alert, settings)
        print(f"    → 게이트 {'열림 ✅ (3조수 가동)' if open_ else '닫힘 ⛔ (기록만)'}")

        if not reports:
            print("    → 리포트 0건 (게이트 미통과)")
            continue

        print(f"    → 리포트 {len(reports)}건:")
        for r in reports:
            print("    " + "-" * 60)
            print(r.model_dump_json(indent=2, exclude_none=False))


def main() -> None:
    """CLI 진입점."""
    p = argparse.ArgumentParser(description="관통 데모 (PM 발행기 → 파이프라인 → 리포트)")
    p.add_argument("--mode", default="mix",
                   choices=["mix", "drift", "spike", "storm", "multi", "quiet"])
    p.add_argument("--count", type=int, default=6, help="생성 건수 (기본 6)")
    p.add_argument("--seed", type=int, default=42, help="재현성 시드 (기본 42)")
    args = p.parse_args()
    run(args.mode, args.count, args.seed)


if __name__ == "__main__":
    main()
