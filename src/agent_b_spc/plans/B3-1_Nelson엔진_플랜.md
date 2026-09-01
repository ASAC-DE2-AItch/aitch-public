# Nelson Rule 판정 엔진 (B3-1) Implementation Plan

> **✅ 완료** — 원엔진 N1~N8·버퍼·리셋·trigger 구현(47 tests) + 후속 정리(refactor/nelson-minor-cleanup) + **조합②(결정 B) 반영(PR #23, 2026-07-13)**. 라이브 배선(DB조회·alert·Kafka)은 B3-2/B4-1/D0-2로 분리.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `fdc.raw` 센서값 + B2-1 관리선을 입력으로 Nelson Rule N1~N8을 실시간 판정해 위반(pre-alert) 목록을 내는, Kafka·DB·alert와 분리된 순수·상태 엔진을 구현한다.

**Architecture:** 두 계층 — `nelson_rules.py`(상태 없는 순수 룰 8개 + 레지스트리 + Zones), `nelson_engine.py`(그룹별 버퍼·리셋·edge/level trigger를 관리하는 `NelsonEngine`). 룰은 zones 경계값만 받아 결정 A(분위수)·B(룰 필터)와 분리, 엔진은 인스턴스당 단일 스레드.

**Tech Stack:** Python 3.14, `dataclasses`·`collections.deque`·`datetime`(표준), pytest(신규), numpy(NaN 판정). 상세 설계: `src/agent_b_spc/specs/B3-1_Nelson엔진_설계.md`.

## Global Constraints

- **센서·그룹 키는 C코드 그대로** (`C11` 등) — 발표명 하드코딩 금지 (헌법 6-4).
- **`print()` 금지 — `logging` 사용** (헌법 6-1). 모든 함수에 docstring (헌법 6-1).
- **매직 넘버 금지** — `TIME_GAP_THRESHOLD_HOURS`는 신규 config 파라미터(아직 `config/params.yaml` 미등재)라 모듈 상수 + `# TODO: config 등재(PM 협의)` 표기.
- **Nelson σ-존은 정의상 1/2/3σ 고정 (K_SIGMA 무관)** — 관리한계 배수 `K_SIGMA`(A1)는 B2-1의 `ucl/lcl`에만 반영되고, 엔진은 그 `ucl/lcl`을 위반 객체에 그대로 담는다. 엔진 zones는 K를 받지 않는다.
- **timestamp = ISO 8601 UTC ("Z")** passthrough (`fdc.raw`/`fdc.alert` 계약 정렬).
- **입력 실패는 스킵 + 로그, 절대 크래시 안 함** (헌법 6-2 정신).
- 확정 해석 잠금: **N1 level-trigger / N2~N8 edge-trigger**, **N5·N6 무가드**, **N8 전부밖+양쪽**, **초과/밖 strict·이내 inclusive**, **N2 ==center → 단절**, **N3 strict 단조·N4 delta≠0**.
- 파일 위치: 코드 `src/agent_b_spc/`, 테스트 `src/agent_b_spc/tests/`. (docs/는 PM 소유라 스펙·플랜은 B 구역.)

---

## File Structure

- `src/agent_b_spc/nelson_rules.py` — `Zones`, 룰 8개(`rule_n1`~`rule_n8`), `RuleSpec`, `RULES`, `MAX_WINDOW`. 순수·무상태.
- `src/agent_b_spc/nelson_engine.py` — `Point`, `BufPoint`, `Violation`, `GroupState`, `NelsonEngine`. 상태.
- `src/agent_b_spc/tests/__init__.py` — 빈 파일.
- `src/agent_b_spc/tests/test_nelson_rules.py` — 룰 8개 유닛테스트(8/8 DoD) + 경계·동점.
- `src/agent_b_spc/tests/test_nelson_engine.py` — 엔진 상태 테스트.
- `requirements.txt` — `pytest>=8.0` 추가.

**범위 밖(별도 task)**: fdc.raw 구독·Kafka 배선(D0-2), spc_violations 적재(B3-2), fdc.alert 발행·Context Score(B4-1). 엔진은 `limits`·`whitelist`를 **주입**받고, 테스트는 합성 데이터로 구동한다.

---

### Task 1: Zones + N1 룰 + 테스트 기반

**Files:**
- Create: `src/agent_b_spc/nelson_rules.py`
- Create: `src/agent_b_spc/tests/__init__.py`
- Create: `src/agent_b_spc/tests/test_nelson_rules.py`
- Modify: `requirements.txt` (pytest 추가)

**Interfaces:**
- Produces: `Zones(center, s1p, s1m, s2p, s2m, s3p, s3m)` frozen dataclass + `Zones.from_limit(center, sigma)`; `rule_n1(values: list[float], zones: Zones) -> bool`.

- [x] **Step 1: pytest 설치 + requirements 갱신**

`requirements.txt` 끝에 한 줄 추가:
```
pytest>=8.0                     # Nelson 엔진 유닛테스트 (B3-1)
```
설치:
```bash
pip install "pytest>=8.0"
```
Expected: `Successfully installed pytest-8.x`

- [x] **Step 2: 실패 테스트 작성**

`src/agent_b_spc/tests/__init__.py` = 빈 파일 생성. `src/agent_b_spc/tests/test_nelson_rules.py`:
```python
"""Nelson 룰 순수 함수 유닛테스트 (B3-1 DoD 8/8)."""
from src.agent_b_spc.nelson_rules import Zones, rule_n1

# 표준 zones: center=0, σ=1 → s1=±1, s2=±2, s3=±3
Z = Zones.from_limit(center=0.0, sigma=1.0)


def test_n1_beyond_3sigma_fires():
    assert rule_n1([3.5], Z) is True      # +3σ 밖
    assert rule_n1([-3.5], Z) is True     # -3σ 밖


def test_n1_within_returns_false():
    assert rule_n1([2.9], Z) is False


def test_n1_exact_boundary_is_strict_false():
    assert rule_n1([3.0], Z) is False     # 정확히 s3p → 위반 아님(strict)


def test_n1_empty_returns_false():
    assert rule_n1([], Z) is False
```

- [x] **Step 3: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_rules.py -v`
Expected: FAIL — `ModuleNotFoundError` 또는 `ImportError: cannot import name 'Zones'`

- [x] **Step 4: 최소 구현**

`src/agent_b_spc/nelson_rules.py`:
```python
"""Nelson Rule N1~N8 — 순수 판정 함수 + 레지스트리 (상태 없음).

각 rule_nX(values, zones)는 values(오래된→최신, 최신이 마지막)의 최신 점에서
끝나는 패턴 존재 시 True를 반환한다. 점이 부족하면 False. 설계:
src/agent_b_spc/specs/B3-1_Nelson엔진_설계.md
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Zones:
    """관리선 경계값 묶음 (center + ±1/2/3σ). 룰은 이 경계만 본다 (결정 A 분리)."""

    center: float
    s1p: float
    s1m: float
    s2p: float
    s2m: float
    s3p: float
    s3m: float

    @classmethod
    def from_limit(cls, center: float, sigma: float) -> "Zones":
        """center·sigma에서 ±1/2/3σ 경계를 계산한다. Nelson zone은 1/2/3σ 고정(K 무관).

        관리한계 배수 K_SIGMA는 B2-1의 ucl/lcl에만 반영되며 여기 zone과는 무관하다.
        (결정 A 분위수 채택 시 이 메서드만 분위수 경계로 교체.)
        """
        return cls(
            center=center,
            s1p=center + sigma, s1m=center - sigma,
            s2p=center + 2 * sigma, s2m=center - 2 * sigma,
            s3p=center + 3 * sigma, s3m=center - 3 * sigma,
        )


def rule_n1(values: list[float], zones: Zones) -> bool:
    """N1: 최신 1점이 ±3σ 밖(strict) 이면 True."""
    if not values:
        return False
    v = values[-1]
    return v > zones.s3p or v < zones.s3m
```

- [x] **Step 5: 통과 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_rules.py -v`
Expected: 4 passed

- [x] **Step 6: 커밋**

```bash
git add requirements.txt src/agent_b_spc/nelson_rules.py src/agent_b_spc/tests/
git commit -m "feat: Nelson Zones + N1 룰 + 테스트 기반 (B3-1)"
```

---

### Task 2: N2(center) + N3(strict 단조)

**Files:**
- Modify: `src/agent_b_spc/nelson_rules.py`
- Modify: `src/agent_b_spc/tests/test_nelson_rules.py`

**Interfaces:**
- Consumes: `Zones`.
- Produces: `rule_n2(values, zones) -> bool` (9점 한쪽, ==center 단절), `rule_n3(values, zones) -> bool` (6점 strict 단조).

- [x] **Step 1: 실패 테스트 추가**

`test_nelson_rules.py` 끝에:
```python
from src.agent_b_spc.nelson_rules import rule_n2, rule_n3


def test_n2_nine_same_side_fires():
    assert rule_n2([0.5] * 9, Z) is True       # 9점 전부 center 위
    assert rule_n2([-0.5] * 9, Z) is True


def test_n2_eight_only_false():
    assert rule_n2([0.5] * 8, Z) is False


def test_n2_middle_opposite_breaks():
    assert rule_n2([0.5, 0.5, 0.5, 0.5, -0.5, 0.5, 0.5, 0.5, 0.5], Z) is False


def test_n2_middle_equals_center_breaks():
    # ==center는 위도 아래도 아님 → run 단절 (일관성 잠금)
    assert rule_n2([0.5, 0.5, 0.5, 0.5, 0.0, 0.5, 0.5, 0.5, 0.5], Z) is False


def test_n3_six_monotonic_up_fires():
    assert rule_n3([1, 2, 3, 4, 5, 6], Z) is True
    assert rule_n3([6, 5, 4, 3, 2, 1], Z) is True


def test_n3_tie_breaks_strict():
    assert rule_n3([1, 2, 3, 3, 4, 5], Z) is False   # 동점 → strict 단절
```

- [x] **Step 2: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_rules.py -v`
Expected: FAIL — `ImportError: cannot import name 'rule_n2'`

- [x] **Step 3: 구현 추가**

`nelson_rules.py`의 `rule_n1` 아래에:
```python
def rule_n2(values: list[float], zones: Zones) -> bool:
    """N2: 최근 9점이 전부 center 위 또는 전부 아래(strict). ==center는 단절."""
    if len(values) < 9:
        return False
    w = values[-9:]
    return all(v > zones.center for v in w) or all(v < zones.center for v in w)


def rule_n3(values: list[float], zones: Zones) -> bool:
    """N3: 최근 6점이 strict 단조 증가 또는 감소. 동점(==)이면 단절."""
    if len(values) < 6:
        return False
    w = values[-6:]
    up = all(w[i] > w[i - 1] for i in range(1, 6))
    down = all(w[i] < w[i - 1] for i in range(1, 6))
    return up or down
```

- [x] **Step 4: 통과 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_rules.py -v`
Expected: 10 passed

- [x] **Step 5: 커밋**

```bash
git add src/agent_b_spc/nelson_rules.py src/agent_b_spc/tests/test_nelson_rules.py
git commit -m "feat: Nelson N2(center 경계)·N3(strict 단조) 룰"
```

---

### Task 3: N4(교대) + N5(2/3 2σ 무가드) + N6(4/5 1σ)

**Files:**
- Modify: `src/agent_b_spc/nelson_rules.py`
- Modify: `src/agent_b_spc/tests/test_nelson_rules.py`

**Interfaces:**
- Consumes: `Zones`.
- Produces: `rule_n4`(14점 교대, delta≠0), `rule_n5`(3점 중 2점 2σ밖, 무가드), `rule_n6`(5점 중 4점 1σ밖).

- [x] **Step 1: 실패 테스트 추가**

```python
from src.agent_b_spc.nelson_rules import rule_n4, rule_n5, rule_n6


def test_n4_alternating_fires():
    zig = [10, 0, 10, 0, 10, 0, 10, 0, 10, 0, 10, 0, 10, 0]  # 14점 교대
    assert rule_n4(zig, Z) is True


def test_n4_delta_zero_breaks():
    w = [10, 0, 10, 0, 0, 0, 10, 0, 10, 0, 10, 0, 10, 0]     # delta=0 낌
    assert rule_n4(w, Z) is False


def test_n5_two_of_three_beyond_2sigma_fires():
    assert rule_n5([2.5, 2.5, 0.0], Z) is True    # 무가드: 최신 정상이어도 True


def test_n5_one_only_false():
    assert rule_n5([2.5, 0.0, 0.0], Z) is False


def test_n6_four_of_five_beyond_1sigma_fires():
    assert rule_n6([1.5, 1.5, 1.5, 1.5, 0.0], Z) is True


def test_n6_three_only_false():
    assert rule_n6([1.5, 1.5, 1.5, 0.0, 0.0], Z) is False
```

- [x] **Step 2: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_rules.py -v`
Expected: FAIL — `ImportError: cannot import name 'rule_n4'`

- [x] **Step 3: 구현 추가**

```python
def rule_n4(values: list[float], zones: Zones) -> bool:
    """N4: 최근 14점의 방향이 매번 교대(지그재그). delta 0이면 교대 깨짐."""
    if len(values) < 14:
        return False
    w = values[-14:]
    diffs = [w[i] - w[i - 1] for i in range(1, 14)]   # 13개
    if any(d == 0 for d in diffs):
        return False
    return all((diffs[i] > 0) != (diffs[i - 1] > 0) for i in range(1, 13))


def rule_n5(values: list[float], zones: Zones) -> bool:
    """N5: 최근 3점 중 2점 이상이 +2σ 밖(또는 2점 이상이 −2σ 밖). 무가드."""
    if len(values) < 3:
        return False
    w = values[-3:]
    above = sum(1 for v in w if v > zones.s2p)
    below = sum(1 for v in w if v < zones.s2m)
    return above >= 2 or below >= 2


def rule_n6(values: list[float], zones: Zones) -> bool:
    """N6: 최근 5점 중 4점 이상이 +1σ 밖(또는 4점 이상이 −1σ 밖)."""
    if len(values) < 5:
        return False
    w = values[-5:]
    above = sum(1 for v in w if v > zones.s1p)
    below = sum(1 for v in w if v < zones.s1m)
    return above >= 4 or below >= 4
```

- [x] **Step 4: 통과 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_rules.py -v`
Expected: 16 passed

- [x] **Step 5: 커밋**

```bash
git add src/agent_b_spc/nelson_rules.py src/agent_b_spc/tests/test_nelson_rules.py
git commit -m "feat: Nelson N4(교대)·N5(무가드)·N6 룰"
```

---

### Task 4: N7(15 이내 inclusive) + N8(8 밖 양쪽 strict)

**Files:**
- Modify: `src/agent_b_spc/nelson_rules.py`
- Modify: `src/agent_b_spc/tests/test_nelson_rules.py`

**Interfaces:**
- Consumes: `Zones`.
- Produces: `rule_n7`(15점 ±1σ 이내, inclusive), `rule_n8`(8점 ±1σ 밖 strict + 양쪽).

- [x] **Step 1: 실패 테스트 추가**

```python
from src.agent_b_spc.nelson_rules import rule_n7, rule_n8


def test_n7_fifteen_within_fires():
    assert rule_n7([0.5] * 15, Z) is True


def test_n7_one_outside_breaks():
    w = [0.5] * 14 + [1.5]
    assert rule_n7(w, Z) is False


def test_n7_exact_1sigma_is_inclusive():
    # 정확히 1σ 값 → 이내(포함)
    assert rule_n7([1.0] * 15, Z) is True


def test_n8_eight_outside_both_sides_fires():
    w = [1.5, -1.5, 1.5, -1.5, 1.5, -1.5, 1.5, -1.5]   # 8점 전부 밖 + 양쪽
    assert rule_n8(w, Z) is True


def test_n8_one_side_only_false():
    assert rule_n8([1.5] * 8, Z) is False              # 전부 밖이나 한쪽만


def test_n8_exact_1sigma_is_strict_not_outside():
    # 정확히 1σ 값은 '밖' 아님 → N8 단절 (N7과 상보)
    w = [1.0, -1.5, 1.5, -1.5, 1.5, -1.5, 1.5, -1.5]
    assert rule_n8(w, Z) is False
```

- [x] **Step 2: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_rules.py -v`
Expected: FAIL — `ImportError: cannot import name 'rule_n7'`

- [x] **Step 3: 구현 추가**

```python
def rule_n7(values: list[float], zones: Zones) -> bool:
    """N7: 최근 15점이 전부 ±1σ 이내(inclusive — 경계값 포함). 층화 감지."""
    if len(values) < 15:
        return False
    w = values[-15:]
    return all(zones.s1m <= v <= zones.s1p for v in w)


def rule_n8(values: list[float], zones: Zones) -> bool:
    """N8: 최근 8점이 전부 ±1σ 밖(strict) 이고 위·아래 양쪽에 분포(혼합)."""
    if len(values) < 8:
        return False
    w = values[-8:]
    if not all(v > zones.s1p or v < zones.s1m for v in w):   # 전부 밖(strict)
        return False
    has_above = any(v > zones.s1p for v in w)
    has_below = any(v < zones.s1m for v in w)
    return has_above and has_below
```

- [x] **Step 4: 통과 확인 (룰 8종 전부)**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_rules.py -v`
Expected: 22 passed

- [x] **Step 5: 커밋**

```bash
git add src/agent_b_spc/nelson_rules.py src/agent_b_spc/tests/test_nelson_rules.py
git commit -m "feat: Nelson N7(inclusive)·N8(양쪽 strict) 룰 — 8/8 룰 완성"
```

---

### Task 5: RuleSpec + RULES 레지스트리

**Files:**
- Modify: `src/agent_b_spc/nelson_rules.py`
- Modify: `src/agent_b_spc/tests/test_nelson_rules.py`

**Interfaces:**
- Produces: `RuleSpec(id, fn, window, severity, zone, trigger)` frozen dataclass; `RULES: list[RuleSpec]` (8개); `MAX_WINDOW: int` (=15).

- [x] **Step 1: 실패 테스트 추가**

```python
from src.agent_b_spc.nelson_rules import RULES, MAX_WINDOW


def test_registry_has_eight_rules():
    assert [r.id for r in RULES] == ["N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8"]


def test_registry_triggers():
    trig = {r.id: r.trigger for r in RULES}
    assert trig["N1"] == "level"                       # 기획서 4-3
    assert all(trig[f"N{i}"] == "edge" for i in range(2, 9))


def test_max_window_is_15():
    assert MAX_WINDOW == 15                             # N7 윈도우
```

- [x] **Step 2: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_rules.py -v`
Expected: FAIL — `ImportError: cannot import name 'RULES'`

- [x] **Step 3: 구현 추가**

`nelson_rules.py` 끝(룰 함수들 아래)에:
```python
from typing import Callable


@dataclass(frozen=True)
class RuleSpec:
    """룰 메타 — 엔진이 룰을 순회·필터하는 단위 (결정 B는 이 리스트 필터로 처리)."""

    id: str
    fn: Callable[[list, Zones], bool]
    window: int
    severity: str          # CRITICAL / WARNING / INFO
    zone: str              # 3sigma / center / none / 2sigma / 1sigma
    trigger: str           # level(N1) / edge(N2~N8)


RULES: list[RuleSpec] = [
    RuleSpec("N1", rule_n1, 1, "CRITICAL", "3sigma", "level"),
    RuleSpec("N2", rule_n2, 9, "WARNING", "center", "edge"),
    RuleSpec("N3", rule_n3, 6, "WARNING", "none", "edge"),
    RuleSpec("N4", rule_n4, 14, "WARNING", "none", "edge"),
    RuleSpec("N5", rule_n5, 3, "WARNING", "2sigma", "edge"),
    RuleSpec("N6", rule_n6, 5, "INFO", "1sigma", "edge"),
    RuleSpec("N7", rule_n7, 15, "INFO", "1sigma", "edge"),
    RuleSpec("N8", rule_n8, 8, "WARNING", "1sigma", "edge"),
]

MAX_WINDOW: int = max(r.window for r in RULES)   # 15 (N7)
```

- [x] **Step 4: 통과 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_rules.py -v`
Expected: 25 passed

- [x] **Step 5: 커밋**

```bash
git add src/agent_b_spc/nelson_rules.py src/agent_b_spc/tests/test_nelson_rules.py
git commit -m "feat: RuleSpec + RULES 레지스트리 + MAX_WINDOW"
```

---

### Task 6: 엔진 골격 — 데이터 타입 + 화이트리스트 필터 + 버퍼 + 입력 방어

**Files:**
- Create: `src/agent_b_spc/nelson_engine.py`
- Create: `src/agent_b_spc/tests/test_nelson_engine.py`

**Interfaces:**
- Consumes: `RULES`, `MAX_WINDOW`, `Zones` from `nelson_rules`.
- Produces:
  - `Point(group_key, wafer_id, timestamp, value, pm_count, limit_version)` dataclass.
  - `NelsonEngine(limits: dict, whitelist: set, active_rules=RULES, time_gap_hours=72)`; `.feed(point) -> list[dict]`.
  - `limits`: `dict[tuple, dict]` — group_key → `{"center","sigma","ucl","lcl","limit_version"}`.
  - group_key = `(chamber_id, recipe_id, step, sensor_window, sensor_id)`.

- [x] **Step 1: 실패 테스트 작성**

`src/agent_b_spc/tests/test_nelson_engine.py`:
```python
"""NelsonEngine 상태 테스트 (합성 데이터, Kafka·DB 불필요)."""
import math

from src.agent_b_spc.nelson_engine import NelsonEngine, Point

GK = ("SIM_CH_1", "C6_0", 4, "settled", "C11")   # 표준 그룹 키
LIMITS = {GK: {"center": 0.0, "sigma": 1.0, "ucl": 3.0, "lcl": -3.0, "limit_version": "v1"}}
WL = {GK}


def _pt(value, wafer="W1", ts="2026-07-13T10:00:00.000Z", pm=1, ver="v1"):
    return Point(group_key=GK, wafer_id=wafer, timestamp=ts, value=value, pm_count=pm, limit_version=ver)


def test_non_whitelisted_group_skipped():
    eng = NelsonEngine(limits=LIMITS, whitelist=set())   # 빈 화이트리스트
    assert eng.feed(_pt(3.5)) == []


def test_buffer_accumulates_and_evaluates():
    # 화이트리스트에 있으면 처리되어 N1(3σ 밖)이 발행됨
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    out = eng.feed(_pt(3.5))
    assert len(out) == 1 and out[0]["rule_id"] == "N1"


def test_nan_value_skipped_no_crash():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    assert eng.feed(_pt(math.nan)) == []


def test_out_of_order_timestamp_dropped():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    eng.feed(_pt(0.0, ts="2026-07-13T10:00:00.000Z"))
    # 더 과거 timestamp → 드롭 (버퍼 불변, 위반 없음)
    assert eng.feed(_pt(3.5, ts="2026-07-13T09:00:00.000Z")) == []
```

- [x] **Step 2: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.agent_b_spc.nelson_engine'`

- [x] **Step 3: 엔진 골격 구현**

`src/agent_b_spc/nelson_engine.py`:
```python
"""NelsonEngine — 그룹별 버퍼·리셋·trigger로 위반을 생성하는 상태 엔진 (B3-1).

인스턴스당 단일 스레드. limits·whitelist는 주입받고, Kafka·DB·alert와 분리.
설계: src/agent_b_spc/specs/B3-1_Nelson엔진_설계.md
"""

from __future__ import annotations

import logging
import math
from collections import deque, namedtuple
from dataclasses import dataclass
from datetime import datetime

from src.agent_b_spc.nelson_rules import RULES, MAX_WINDOW, Zones

logger = logging.getLogger(__name__)

# TODO: config/params.yaml 등재(PM 협의) — 신규 파라미터. >53h(관측 최대 양성 공백)
TIME_GAP_THRESHOLD_HOURS = 72

BufPoint = namedtuple("BufPoint", "wafer_id value timestamp")


@dataclass
class Point:
    """엔진 입력 — wafer 요약점 1개 (상류가 fdc.raw에서 시간순 공급)."""

    group_key: tuple
    wafer_id: str
    timestamp: str        # ISO 8601 UTC ("Z")
    value: float
    pm_count: int
    limit_version: str


class _GroupState:
    """그룹별 상태 — 점 버퍼(최대 MAX_WINDOW) + 리셋 추적 + 규칙별 에피소드."""

    def __init__(self) -> None:
        self.buffer: deque = deque(maxlen=MAX_WINDOW)
        self.last_pm_count: int | None = None
        self.last_limit_version: str | None = None
        self.last_dt: datetime | None = None
        self.episodes: dict[str, bool] = {}


class NelsonEngine:
    """Nelson 판정 엔진. feed(point) → 새로 발행된 위반 목록."""

    def __init__(self, limits: dict, whitelist: set,
                 active_rules=RULES, time_gap_hours: float = TIME_GAP_THRESHOLD_HOURS) -> None:
        """limits·whitelist 주입. 화이트리스트 각 그룹의 관리선 존재를 __init__에서 점검."""
        self.limits = limits
        self.whitelist = set(whitelist)
        self.active_rules = active_rules
        self.time_gap_sec = time_gap_hours * 3600
        self._state: dict[tuple, _GroupState] = {}
        for gk in self.whitelist:
            if gk not in self.limits:
                logger.warning("화이트리스트 그룹에 관리선 없음(config 갭): %s", gk)

    def _zones(self, gk: tuple) -> Zones:
        """그룹 관리선에서 Zones 계산 (결정 A 오면 이 메서드만 교체)."""
        lim = self.limits[gk]
        return Zones.from_limit(lim["center"], lim["sigma"])

    def feed(self, point: Point) -> list[dict]:
        """점 1개 처리 → 새로 발행된 위반 목록. (규칙 평가는 Task 7에서 추가)"""
        gk = point.group_key
        if gk not in self.whitelist:
            return []
        # 입력 방어
        if point.value is None or (isinstance(point.value, float) and math.isnan(point.value)):
            logger.warning("값 결측/NaN — 점 스킵: wafer=%s", point.wafer_id)
            return []
        if gk not in self.limits:
            logger.error("관리선 없는 화이트리스트 그룹 — 스킵: %s", gk)
            return []
        try:
            dt = datetime.fromisoformat(point.timestamp)
        except (ValueError, TypeError):
            logger.warning("timestamp 파싱 실패 — 점 스킵: wafer=%s ts=%s", point.wafer_id, point.timestamp)
            return []
        st = self._state.setdefault(gk, _GroupState())
        if st.last_dt is not None and dt < st.last_dt:
            logger.warning("timestamp 역전 — 드롭: wafer=%s", point.wafer_id)
            return []
        # 버퍼 적재
        st.buffer.append(BufPoint(point.wafer_id, point.value, point.timestamp))
        st.last_dt = dt
        st.last_pm_count = point.pm_count
        st.last_limit_version = point.limit_version
        # 규칙 평가·trigger는 Task 7
        return self._evaluate(gk, st)

    def _evaluate(self, gk: tuple, st: _GroupState) -> list[dict]:
        """규칙 평가 + trigger (Task 7에서 구현). 지금은 빈 목록."""
        return []
```

- [x] **Step 4: 부분 통과 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_engine.py -v`
Expected: `test_non_whitelisted_group_skipped`·`test_nan_value_skipped_no_crash`·`test_out_of_order_timestamp_dropped` PASS, `test_buffer_accumulates_and_evaluates` FAIL (아직 `_evaluate`가 빈 목록 — N1 미발행). 이 FAIL은 Task 7에서 해소.

- [x] **Step 5: 커밋**

```bash
git add src/agent_b_spc/nelson_engine.py src/agent_b_spc/tests/test_nelson_engine.py
git commit -m "feat: NelsonEngine 골격 — 타입·화이트리스트·버퍼·입력방어"
```

---

### Task 7: 규칙 평가 + trigger(N1 level / N2~N8 edge) + 위반 객체

**Files:**
- Modify: `src/agent_b_spc/nelson_engine.py` (`_evaluate` 구현 + `_make_violation`)
- Modify: `src/agent_b_spc/tests/test_nelson_engine.py`

**Interfaces:**
- Produces: `_evaluate`가 위반 dict 목록 반환. 위반 dict 키: `chamber_id, recipe_id, step, sensor_window, sensor_id, rule_id, severity, current_value, control_limit_upper, control_limit_lower, limit_version, limit_basis, description, wafer_id, timestamp, offending`.

- [x] **Step 1: 실패 테스트 추가**

`test_nelson_engine.py` 끝에:
```python
from src.agent_b_spc.nelson_rules import RULES

_N1_ONLY = [r for r in RULES if r.id == "N1"]


def _feed_seq(eng, values, ver="v1", pm=1, base_min=0):
    """값 시퀀스를 시간 증가시키며 feed, 마지막 반환값을 돌려준다."""
    out = []
    for i, v in enumerate(values):
        ts = f"2026-07-13T10:{base_min + i:02d}:00.000Z"
        out = eng.feed(_pt(v, wafer=f"W{i}", ts=ts, pm=pm, ver=ver))
    return out


def test_n1_level_trigger_fires_every_point():
    # 기획서 4-3: 3σ 밖 연속 3장 → 3건 모두 발행 (level). N1만 활성화해 N5 오염 격리.
    eng = NelsonEngine(limits=LIMITS, whitelist=WL, active_rules=_N1_ONLY)
    counts = [len(_feed_seq(eng, [v], base_min=i)) for i, v in enumerate([3.5, 3.6, 3.7])]
    assert counts == [1, 1, 1]


def test_n2_edge_trigger_once_at_ninth():
    # 8점→미발행, 9점→N2 최초 1회 (edge)
    def n2_fired(n):
        out = _feed_seq(NelsonEngine(limits=LIMITS, whitelist=WL), [0.5] * n)
        return any(x["rule_id"] == "N2" for x in out)
    assert n2_fired(8) is False
    assert n2_fired(9) is True


def test_violation_fields_and_offending():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    out = _feed_seq(eng, [2.5, 2.5, 0.0])             # N5 발화 (무가드, 최신 정상점)
    v = next(x for x in out if x["rule_id"] == "N5")
    assert v["sensor_id"] == "C11"
    assert v["severity"] == "WARNING"
    assert v["control_limit_upper"] == 3.0
    assert v["limit_basis"] == "firm"
    assert len(v["offending"]) >= 2                   # 2σ 밖 점들 (귀속)


def test_description_carries_run_range():
    # N2/N3/N4는 offending 대신 description이 run 범위(첫..끝)를 담아야 자기완결
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    out = _feed_seq(eng, [1, 2, 3, 4, 5, 6])          # N3 6점 단조↑
    v = next(x for x in out if x["rule_id"] == "N3")
    assert ".." in v["description"] and "W0" in v["description"]   # 예: "N3: 6점 창 (W0..W5)"


def test_sigma_zero_skips_zone_rules():
    # σ=0(붕괴 zone)에서 center를 크게 벗어나도 N1(3σ) 헛발동 안 함 (defense-in-depth)
    limits0 = {GK: {"center": 5.0, "sigma": 0.0, "ucl": 5.0, "lcl": 5.0, "limit_version": "v1"}}
    eng = NelsonEngine(limits=limits0, whitelist=WL)
    out = eng.feed(_pt(100.0))
    assert not any(x["rule_id"] == "N1" for x in out)
```

- [x] **Step 2: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_engine.py::test_n1_level_trigger_fires_every_point -v`
Expected: FAIL (assert `[0,0,0] == [1,1,1]` — `_evaluate` 빈 목록)

- [x] **Step 3: `_evaluate` + `_make_violation` 구현**

`nelson_engine.py`의 `_evaluate`를 아래로 교체하고 `_make_violation` 추가:
```python
    def _evaluate(self, gk: tuple, st: _GroupState) -> list[dict]:
        """활성 룰을 버퍼에 평가하고 trigger(N1 level / 나머지 edge)로 위반 생성.

        σ≤0(붕괴)이면 σ-존 룰(1/2/3σ 의존)을 스킵한다 — zones가 center로 붕괴해
        헛발동(N1 폭풍)하는 것 방지 (스펙 §5 defense-in-depth). N2(center)/N3/N4는 무관.
        """
        zones = self._zones(gk)
        values = [bp.value for bp in st.buffer]
        sigma_ok = self.limits[gk]["sigma"] > 0
        if not sigma_ok:
            logger.warning("σ=0 그룹 — σ-존 룰 스킵(헛발동 방지): %s", gk)
        emitted: list[dict] = []
        for spec in self.active_rules:
            if not sigma_ok and spec.zone in ("1sigma", "2sigma", "3sigma"):
                continue
            fired = spec.fn(values, zones)
            if spec.trigger == "level":
                if fired:
                    emitted.append(self._make_violation(gk, st, spec, zones))
            else:  # edge
                prev = st.episodes.get(spec.id, False)
                if fired and not prev:
                    emitted.append(self._make_violation(gk, st, spec, zones))
                st.episodes[spec.id] = fired
        return emitted

    def _make_violation(self, gk, st, spec, zones) -> dict:
        """위반 dict 생성 — spc_violations 매핑 + offending(N5/N6) + limit_basis + run 서술."""
        chamber_id, recipe_id, step, sensor_window, sensor_id = gk
        lim = self.limits[gk]
        latest = st.buffer[-1]
        win = list(st.buffer)[-spec.window:]
        w_range = f"{win[0].wafer_id}..{win[-1].wafer_id}" if len(win) > 1 else latest.wafer_id
        # offending: 귀속 애매한 N5/N6에 채움 (해당 zone 밖 점들)
        offending = []
        if spec.id in ("N5", "N6"):
            edge_p = zones.s2p if spec.id == "N5" else zones.s1p
            edge_m = zones.s2m if spec.id == "N5" else zones.s1m
            offending = [{"wafer_id": bp.wafer_id, "value": bp.value}
                         for bp in win if bp.value > edge_p or bp.value < edge_m]
        return {
            "chamber_id": chamber_id, "recipe_id": recipe_id, "step": step,
            "sensor_window": sensor_window, "sensor_id": sensor_id,
            "rule_id": spec.id, "severity": spec.severity,
            "current_value": latest.value,
            "control_limit_upper": lim["ucl"], "control_limit_lower": lim["lcl"],
            "limit_version": lim["limit_version"],
            "limit_basis": "firm",   # 가한계 대비 예약 (W5 활성)
            "description": f"{spec.id}: {spec.window}점 창 ({w_range})",   # run 범위 = 자기완결
            "wafer_id": latest.wafer_id, "timestamp": latest.timestamp,
            "offending": offending,
        }
```

- [x] **Step 4: 통과 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_engine.py -v`
Expected: Task 6·7 테스트 전부 PASS (`test_buffer_accumulates_and_evaluates` 포함)

- [x] **Step 5: 커밋**

```bash
git add src/agent_b_spc/nelson_engine.py src/agent_b_spc/tests/test_nelson_engine.py
git commit -m "feat: 규칙 평가 + trigger(N1 level/N2~8 edge) + 위반 객체"
```

---

### Task 8: 리셋 3종 — pm_count·time-gap(비움) / limit_version(silent re-baseline)

**Files:**
- Modify: `src/agent_b_spc/nelson_engine.py` (`feed`에 리셋 판정 삽입 + `_rebaseline`)
- Modify: `src/agent_b_spc/tests/test_nelson_engine.py`

**Interfaces:**
- Consumes: `_GroupState`, `_evaluate`, `_zones`.
- Produces: `feed`가 pm_count 증가·time-gap 초과 시 버퍼·에피소드 비움; limit_version 변경 시 버퍼 유지 + zones 재계산 + 에피소드 silent re-baseline.

- [x] **Step 1: 실패 테스트 추가**

```python
def test_reset_on_pm_count_clears_buffer():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    _feed_seq(eng, [0.5] * 8, pm=1)                    # N2 직전(8점)
    out = eng.feed(_pt(0.5, wafer="W8", ts="2026-07-13T10:20:00.000Z", pm=2))  # PM → 리셋
    # 리셋으로 버퍼가 1점 → N2(9점) 불성립
    assert not any(x["rule_id"] == "N2" for x in out)


def test_reset_on_time_gap_clears_buffer():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL, time_gap_hours=12)
    _feed_seq(eng, [0.5] * 8)                           # 10:00~10:07
    # 24h 뒤 점 → time-gap(>12h) 리셋
    out = eng.feed(_pt(0.5, wafer="W8", ts="2026-07-14T10:20:00.000Z"))
    assert not any(x["rule_id"] == "N2" for x in out)


def test_limit_version_change_no_spurious_fire():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    # old center 0에서 mixed(±0.2) 9점 → N2 False(양쪽), 다른 룰도 미발화(전부 1σ 이내)
    mixed = [0.2, -0.2, 0.2, -0.2, 0.2, -0.2, 0.2, -0.2, 0.2]
    assert not _feed_seq(eng, mixed)                   # 발행 없음
    # 관리선 갱신: center 0.5로 이동 + v2 → ±0.2가 전부 새 center '아래'로 뒤집힘
    eng.limits = {GK: {"center": 0.5, "sigma": 1.0, "ucl": 3.5, "lcl": -2.5, "limit_version": "v2"}}
    out = eng.feed(_pt(0.2, wafer="W9", ts="2026-07-13T10:20:00.000Z", ver="v2"))
    # silent re-baseline이 recenter로 인한 '9 below' 전이를 흡수 → N2 헛발동 없음
    # (re-baseline 없으면 prev=False라 여기서 N2가 spurious 발행됨)
    assert not any(x["rule_id"] == "N2" for x in out)
```

- [x] **Step 2: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_engine.py -k "reset or version" -v`
Expected: FAIL (리셋 로직 없음 → 버퍼가 안 비워지거나 헛발동)

- [x] **Step 3: `feed`에 리셋 판정 삽입 + `_rebaseline`**

`feed`에서 `st = self._state.setdefault(...)` 직후, **역전 드롭 체크 뒤·버퍼 append 앞**에 삽입:
```python
        # 리셋 판정 (설계 §4)
        if st.last_pm_count is not None and point.pm_count > st.last_pm_count:
            st.buffer.clear(); st.episodes.clear()            # ① PM
        elif st.last_dt is not None and (dt - st.last_dt).total_seconds() > self.time_gap_sec:
            st.buffer.clear(); st.episodes.clear()            # ③ time-gap
        elif st.last_limit_version is not None and point.limit_version != st.last_limit_version:
            self._rebaseline(gk, st)                          # ② limit_version — 버퍼 유지
```
> ⚠️ 위 블록은 `st.last_dt = dt` 재할당보다 **앞**에 와야 한다(직전 dt와 비교). 기존 `st.last_dt = dt`·`st.last_pm_count`·`st.last_limit_version` 갱신은 그대로 append 뒤에 유지.

`_rebaseline` 메서드 추가:
```python
    def _rebaseline(self, gk: tuple, st: _GroupState) -> None:
        """limit_version 변경 시 zones 재계산 + 에피소드 silent 재기준(발행 없음).

        σ≤0이면 σ-존 룰은 재기준에서 제외 — _evaluate와 동일 가드로 두 경로 일관성 유지.
        """
        zones = self._zones(gk)                # 이미 self.limits는 새 버전
        values = [bp.value for bp in st.buffer]
        sigma_ok = self.limits[gk]["sigma"] > 0
        for spec in self.active_rules:
            if spec.trigger != "edge":
                continue
            if not sigma_ok and spec.zone in ("1sigma", "2sigma", "3sigma"):
                continue
            st.episodes[spec.id] = spec.fn(values, zones)
```

- [x] **Step 4: 통과 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_engine.py -v`
Expected: 전부 PASS

- [x] **Step 5: 커밋**

```bash
git add src/agent_b_spc/nelson_engine.py src/agent_b_spc/tests/test_nelson_engine.py
git commit -m "feat: 리셋 3종 — pm_count·time-gap 비움 / limit_version silent re-baseline"
```

---

### Task 9: update_whitelist(축소 삭제) + 경계·해소 핀포인트 테스트

**Files:**
- Modify: `src/agent_b_spc/nelson_engine.py` (`update_whitelist`, `reset`)
- Modify: `src/agent_b_spc/tests/test_nelson_engine.py`

**Interfaces:**
- Produces: `NelsonEngine.update_whitelist(new_set: set) -> None` (축소 시 제외 그룹 `_GroupState` 삭제); `NelsonEngine.reset(group_key) -> None`.

- [x] **Step 1: 실패 테스트 추가**

```python
def test_update_whitelist_shrink_deletes_state():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    eng.feed(_pt(0.5))
    assert GK in eng._state
    eng.update_whitelist(set())                       # 축소 → 제외
    assert GK not in eng._state                        # GroupState 삭제(stale 방지)
    assert eng.feed(_pt(3.5)) == []                    # 이제 비대상


def test_buffer_boundary_n7_at_full_buffer():
    # 16번째 점 유입해도 최근 15점으로 N7 정확 판정 (maxlen≥최장 룰 불변식)
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    _feed_seq(eng, [0.5] * 16)                          # 16점 유입
    assert len(eng._state[GK].buffer) == 15             # maxlen 유지 — 오래된 1점 소멸(불변식)
    # 15점째에서 N7 발행됐는지 (최근 15점으로 정확 판정)
    eng2 = NelsonEngine(limits=LIMITS, whitelist=WL)
    out15 = _feed_seq(eng2, [0.5] * 15)
    assert any(x["rule_id"] == "N7" for x in out15)


def test_n5_clear_pinpoint_rearm():
    eng = NelsonEngine(limits=LIMITS, whitelist=WL)
    _feed_seq(eng, [2.5, 2.5])                          # 2점 — N5 아직(3점 필요)
    eng.feed(_pt(0.0, wafer="Wa", ts="2026-07-13T10:05:00.000Z"))  # [2.5,2.5,0.0]=2/3 → N5 최초 발화
    eng.feed(_pt(0.0, wafer="Wb", ts="2026-07-13T10:06:00.000Z"))  # [2.5,0.0,0.0]=1/3 → 해소·재무장(pinpoint)
    eng.feed(_pt(2.5, wafer="Wc", ts="2026-07-13T10:07:00.000Z"))  # [0.0,0.0,2.5]=1/3 → 미발행
    out = eng.feed(_pt(2.5, wafer="Wd", ts="2026-07-13T10:08:00.000Z"))  # [0.0,2.5,2.5]=2/3 → 재무장됐으므로 재발행
    assert any(x["rule_id"] == "N5" for x in out)      # 해소됐어야만 여기서 재발행됨
```

- [x] **Step 2: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_nelson_engine.py::test_update_whitelist_shrink_deletes_state -v`
Expected: FAIL — `AttributeError: 'NelsonEngine' object has no attribute 'update_whitelist'`

- [x] **Step 3: 메서드 구현**

`NelsonEngine`에 추가:
```python
    def update_whitelist(self, new_set: set) -> None:
        """감시 대상 런타임 교체. 축소 시 제외 그룹 상태 삭제(stale 버퍼·잔류 방지).

        ⚠️ 컨슈머(feed) 스레드에서만 호출 — 외부 트리거는 커맨드 큐로 전달해 feed 루프가
        적용해야 lock-free 성립 (별도 스레드 직접 호출 금지).
        """
        new_set = set(new_set)
        for gk in list(self._state):
            if gk not in new_set:
                del self._state[gk]           # 제외 그룹 GroupState 삭제
        self.whitelist = new_set
        for gk in new_set:
            if gk not in self.limits:
                logger.warning("화이트리스트 그룹에 관리선 없음(config 갭): %s", gk)

    def reset(self, group_key: tuple) -> None:
        """그룹 상태 수동 리셋 (버퍼·에피소드 비움)."""
        self._state.pop(group_key, None)
```

- [x] **Step 4: 통과 확인 (엔진 + 룰 전체)**

Run: `python -m pytest src/agent_b_spc/tests/ -v`
Expected: 전부 PASS (룰 25 + 엔진 테스트)

- [x] **Step 5: 커밋**

```bash
git add src/agent_b_spc/nelson_engine.py src/agent_b_spc/tests/test_nelson_engine.py
git commit -m "feat: update_whitelist(축소 삭제)·reset + 경계·해소 핀포인트 테스트"
```

---

## 완료 판정

- `python -m pytest src/agent_b_spc/tests/ -v` 전부 PASS → **B3-1 DoD(룰 8/8 + 엔진 상태) 충족**.
- 산출: `nelson_rules.py`(순수 룰 8 + 레지스트리), `nelson_engine.py`(상태 엔진), 테스트 2파일.
- **σ=0 defense-in-depth 구현됨** (Task 7) — 화이트리스트가 `zero_sigma`를 제외하지만, 그 불변식이
  깨져도 엔진이 σ-존 룰을 스킵해 헛발동을 막는다 (엔진 correctness가 B2-1에 종속되지 않음, 스펙 §5).
- **후속(범위 밖)**: B2-1 CSV → limits·whitelist 로더 + fdc.raw 구독(D0-2), spc_violations 적재(B3-2), fdc.alert·Context Score(B4-1). 결정 A·B 확정 시 `_zones`·`active_rules`로 국소 반영.
