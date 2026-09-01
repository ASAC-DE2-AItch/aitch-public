"""anchor_constants.json 생성기 — 센서별 정착(settled) 앵커 상수.

용도: fixtures(mock alert) 및 추후 리포트 서술에서 "그럴듯한" 센서 실측값·관리한계를
    결정적으로 만들기 위한 앵커. 매직 넘버를 코드에 흩뿌리지 않기 위한 단일 소스(6-1).

출처(provenance) 원칙 — 수치를 창작하지 않는다(프롬프트 라이브러리 §0 수치 원칙과 동일 정신):
  · fleet_std : config/chamber_offsets.json 의 sensor_std (실데이터 std, 확정)
  · center    : docs/데이터_사전_v1.md(v3) 의 '정착 구간' 관측 + API Contract §3 예시
  · settled_std: **잠정(provisional)** — 정착 구간 전용 σ 는 아직 확정 소스가 없다.
      데이터 사전이 명시한 정착값 폭 + Contract §3 예시(C11: center -310, 관리한계 ±30 → σ≈10)를
      기준으로 |center| 의 소수 % 수준으로 부여. **B 실력치 초기화(B3-1) 산출 시 교체 대상.**
      confidence=provisional 로 명시 표기하여 후속 교체를 강제한다.

⚠️ fleet_std(전 레짐 합산)는 정착 관리한계 계산에 직접 쓰면 폭이 비현실적으로 넓다
   (예: C11 -335 ± 3×143 → 0 을 가로지름). fixtures 의 관리한계는 settled_std 를 쓴다.
   fleet_std 는 참고·교차검증용으로만 병기한다.

재현성: 입력(chamber_offsets.json)과 아래 CENTERS 표가 동일하면 출력 바이트 동일(정렬 dump).
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = Path(__file__).resolve().parents[1]
CHAMBER_OFFSETS = REPO_ROOT / "config" / "chamber_offsets.json"
OUT_PATH = SERVICE_ROOT / "config" / "anchor_constants.json"

# center = 정착 구간 대표값, settled_std = 정착 σ(잠정), window = 대표 관측 창.
# provenance/confidence 는 각 값의 근거·신뢰도.
# (sensor_name 은 표시 계층 전용 — 판정은 C코드. 데이터 사전 v3 발표용 명칭.)
CENTERS: dict[str, dict] = {
    # --- RF·플라즈마 (핵심 판정군) ---
    "C11": {"name": "DC_Bias", "center": -310.0, "settled_std": 10.0, "window": "settled",
            "confidence": "seeded", "prov_center": "API Contract §3 예시(center -310, 한계 ±30)"},
    "C12": {"name": "DC_Bias_Ref", "center": -320.0, "settled_std": 8.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(C11 피크 연동 기준값) — 레짐 신호(손잡이 아님)"},
    "C31": {"name": "RF_Forward", "center": 290.0, "settled_std": 8.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(정착 282→299)"},
    "C32": {"name": "RF_Reflect", "center": 1.5, "settled_std": 1.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(정착 99.4% ≤3)"},
    "C61": {"name": "RF_Vneg", "center": -1090.0, "settled_std": 35.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(정착 |1090| 유지)"},
    "C62": {"name": "RF_Vpp", "center": 1800.0, "settled_std": 55.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(정착 +1800)"},
    "C18": {"name": "Match_Transient", "center": 0.05, "settled_std": 0.5, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(정착 후 0.04)"},
    "C27": {"name": "Match_Residual", "center": 1.0, "settled_std": 2.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(정착 후 소값)"},
    "C54": {"name": "Match_A", "center": 0.0, "settled_std": 60.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(±대칭 준고정 위치값)"},
    "C56": {"name": "Match_B", "center": 0.0, "settled_std": 60.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(±대칭 준고정 위치값)"},
    "C25": {"name": "Baseline_Drift", "center": -0.055, "settled_std": 0.003, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(-0.055 부근 · 노후도 보조)"},
    "C63": {"name": "Aux_63", "center": 224.0, "settled_std": 12.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(step4 내 190→224)"},
    "C1": {"name": "RF_Power_Set", "center": 118.0, "settled_std": 2.0, "window": "settled",
           "confidence": "seeded", "prov_center": "데이터 사전 v3(step1·4=118) — setpoint(손잡이)"},
    # --- 챔버환경·가스·온도 ---
    "C4": {"name": "Gas_Set_A", "center": 40.0, "settled_std": 1.0, "window": "settled",
           "confidence": "seeded", "prov_center": "데이터 사전 v3(C6_0=40) — setpoint(손잡이)"},
    "C5": {"name": "Gas_Set_B", "center": 71.0, "settled_std": 1.5, "window": "settled",
           "confidence": "seeded", "prov_center": "데이터 사전 v3(C6_0=71) — setpoint(손잡이)"},
    "C15": {"name": "Gas_A", "center": 164.0, "settled_std": 5.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(step4 164)"},
    "C16": {"name": "Gas_B_Actual", "center": 1576.0, "settled_std": 50.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(레벨 1576)"},
    "C48": {"name": "Gas_B_Set", "center": 1576.0, "settled_std": 40.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(정착 고정 1576) — setpoint"},
    "C17": {"name": "Chamber_Temp", "center": 200.0, "settled_std": 6.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(프로파일 107→248 대표값) — 레짐 신호"},
    "C9": {"name": "Temp_Aux", "center": 195.0, "settled_std": 6.0, "window": "settled",
           "confidence": "provisional", "prov_center": "데이터 사전 v3(C17 동행 열거동)"},
    "C57": {"name": "He_Valve", "center": 11.0, "settled_std": 1.0, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(median 11)"},
    "C58": {"name": "He_Pressure", "center": 11.98, "settled_std": 0.3, "window": "settled",
            "confidence": "provisional", "prov_center": "데이터 사전 v3(median 11.98~11.99)"},
}


def main() -> None:
    offsets_doc = json.loads(CHAMBER_OFFSETS.read_text(encoding="utf-8"))
    fleet_std = offsets_doc.get("sensor_std", {})

    sensors: dict[str, dict] = {}
    for sid, meta in CENTERS.items():
        sensors[sid] = {
            "name": meta["name"],
            "center": meta["center"],
            "settled_std": meta["settled_std"],
            "fleet_std": fleet_std.get(sid),  # 참고·교차검증용 (없으면 null)
            "window": meta["window"],
            "confidence": meta["confidence"],
            "provenance": {
                "center": meta["prov_center"],
                "settled_std": "잠정 — 정착 σ 확정 소스 없음(B3-1 산출 시 교체). |center| 소수%·Contract §3 기준",
                "fleet_std": "config/chamber_offsets.json sensor_std (실데이터, 확정)",
            },
        }

    doc = {
        "_meta": {
            "purpose": "센서별 정착 앵커 상수 — mock alert(fixtures)·리포트 서술의 결정적 수치 소스",
            "generated_by": "src/agent_service/scripts/build_anchor_constants.py",
            "inputs": [
                "config/chamber_offsets.json (sensor_std=fleet_std)",
                "docs/데이터_사전_v1.md v3 (center 정착 관측)",
                "docs/API_Contract_명세서.md §3 (C11 예시 seed)",
            ],
            "caveats": [
                "settled_std 는 잠정값(confidence=provisional/seeded) — B 실력치 초기화(B3-1)에서 교체.",
                "fleet_std(전 레짐 합산)를 정착 관리한계 계산에 직접 쓰지 말 것(폭 비현실).",
                "판정은 C코드 사용 — name(표시명)은 서술 계층 전용(헌법 6-4).",
            ],
            "chamber_offset_seed": offsets_doc.get("seed"),
        },
        "sensors": dict(sorted(sensors.items())),
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[build_anchor_constants] {len(sensors)} 센서 → {OUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
