# -*- coding: utf-8 -*-
"""M3 ② 파일드롭 주입 채널(InjectControl) 단위 테스트 — Kafka·CSV 무의존.

검증: 드롭→장전→processed 이동 · BOM 내성(PS1 교훈 §7) · 불량 yaml 생존(failed) ·
스캔 스로틀 · 멀티챔버 start 스탬프(max). 실행: python tests/test_inject_control.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from simulator.inject_control import InjectControl  # noqa: E402
from simulator.scenario_injector import ScenarioInjector  # noqa: E402

STD = {"C11": 10.0, "C17": 8.0}

YAML_OK = """scenario_id: LIVE-shift-ch3
pattern: shift
target_chamber: SIM_CH_3
sensor: C11
magnitude_sigma: 3.0
start_after_wafers: 0
ground_truth: live_test
"""


def _ctl(interval=0.0):
    d = Path(tempfile.mkdtemp(prefix="ic_"))
    inj = ScenarioInjector([], STD, events_path=d / "ev.jsonl")
    return InjectControl(d, inj, min_interval_sec=interval), inj, d


def test_drop_inject_and_move():
    """드롭 파일 → 현재 순번으로 장전 + processed/ 이동 (재주입 방지)."""
    ctl, inj, d = _ctl()
    (d / "a.yaml").write_text(YAML_OK, encoding="utf-8")
    got = ctl.poll({"SIM_CH_3": 120})
    assert len(got) == 1 and got[0]["start_after_wafers"] == 120
    assert inj.delta("SIM_CH_3", "C11", 119) == 0.0        # 소급 발동 없음
    assert inj.delta("SIM_CH_3", "C11", 120) == 30.0       # 지금부터 (3σ×10)
    assert not (d / "a.yaml").exists()                     # 원위치에서 사라짐
    assert len(list((d / "processed").iterdir())) == 1     # processed/ 이동
    assert ctl.status()["injected"] == 1


def test_bom_tolerated():
    """PowerShell이 BOM 붙여 저장해도 파싱 (utf-8-sig — 헌법 §7 .ps1 교훈)."""
    ctl, inj, d = _ctl()
    (d / "bom.yaml").write_bytes(b"\xef\xbb\xbf" + YAML_OK.encode("utf-8"))
    assert len(ctl.poll({"SIM_CH_3": 10})) == 1
    assert inj.delta("SIM_CH_3", "C11", 10) == 30.0


def test_bad_yaml_survives_to_failed():
    """불량 파일 = failed/ 이동 + 루프 생존, 옆의 정상 파일은 정상 처리 (헌법 6-2)."""
    ctl, inj, d = _ctl()
    (d / "1bad.yaml").write_text("pattern: shift\n# 필수 키 대거 누락", encoding="utf-8")
    (d / "2ok.yaml").write_text(YAML_OK, encoding="utf-8")
    got = ctl.poll({"SIM_CH_3": 5})
    assert len(got) == 1 and ctl.status()["failed"] == 1
    assert len(list((d / "failed").iterdir())) == 1
    assert inj.delta("SIM_CH_3", "C11", 5) == 30.0         # 정상 파일은 장전됨


def test_scan_throttle():
    """min_interval 내 재호출은 무스캔 — wafer 루프 부하 방어."""
    ctl, inj, d = _ctl(interval=60.0)
    ctl.poll({})                                           # 첫 스캔 (타임스탬프 기록)
    (d / "late.yaml").write_text(YAML_OK, encoding="utf-8")
    assert ctl.poll({"SIM_CH_3": 1}) == []                 # 간격 내 — 스킵
    assert (d / "late.yaml").exists()                      # 아직 대기
    ctl._last_scan = time.monotonic() - (60.0 + 1)         # 간격 경과 강제 (절대값 금지)
    assert len(ctl.poll({"SIM_CH_3": 1})) == 1


def test_multichamber_stamp_max():
    """멀티챔버 대상 = 현재 순번 최대값 스탬프 (뒤처진 챔버 소급 발동 방지)."""
    ctl, inj, d = _ctl()
    y = YAML_OK.replace("target_chamber: SIM_CH_3",
                        "target_chambers: [SIM_CH_1, SIM_CH_2]")
    (d / "multi.yaml").write_text(y, encoding="utf-8")
    got = ctl.poll({"SIM_CH_1": 90, "SIM_CH_2": 91})
    assert got[0]["start_after_wafers"] == 91              # max
    assert inj.delta("SIM_CH_1", "C11", 90) == 0.0         # 뒤처진 챔버는 아직
    assert inj.delta("SIM_CH_1", "C11", 91) == 30.0        # 따라잡으면 발동


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} PASS")
