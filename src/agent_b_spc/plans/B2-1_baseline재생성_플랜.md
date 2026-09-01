# 초기 실력치 baseline 재생성 (B2-1 마무리) Implementation Plan

> **✅ 완료 (2026-07-13, PR #21 머지)** — 전 태스크(Task 1~6·8·9; Task 7 폐기) 구현·검증 완료. 재생성물 77그룹(KEEP 39/KEEP\* 6/EXCLUDE? 4/EXCLUDE 28), 72 tests. 아래 step 체크박스는 실행 추적용으로 **전부 완료됨**.
>
> ⚠️ 후속(2026-07-14): C54/C56 정착→과도 재정정으로 baseline 재생성 → 총 69그룹·EXCLUDE 20(감시 45 불변). 이 문서의 77/28은 PR #21 당시 기록.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 2026-07-11 회의 결정 4건(C6_1 제외·C54/C56 정착 이동·D_CH5960 제외·조합② 분위수 2-track)을 `initial_limits.py`·`monitoring_whitelist.py`에 반영하고 `v1` 산출물을 재생성하며, config 마이그레이션으로 헌법 6-1 잔여 위반을 해소한다.

**Architecture:** 2단 커밋 — **Stage A**(config 이관, 출력 불변 → diff=0 증명) → **Stage B**(4결정 + 죽은 코드 제거 + q 등재 → 기대 diff만). config는 import 부작용이 아니라 호출 시점에 로드해 함수에 주입(hermetic 테스트). 분위수 경계는 **트림 전 표본**에서 산출(트림은 σ-track 전용).

> **리뷰 R6차 정정 (2026-07-13)**: 초안의 Task 7(modality 게이트)은 **제거**. 실데이터서 C54/C56 step4 정착은
> `bimodal=False`(center_density 25~29% > valley 15%)라 게이트가 dead code이고, whitelist 판정은 **KEEP**(광폭
> ±3σ, self-FA 0%)이 회의 결정·데이터와 정합. Task 8 DoD를 step4=KEEP으로 정정. "광폭 밴드 무력"은 W7 백로그
> (스펙 §3-2). Task 1~6은 초안 그대로 유효.

**Tech Stack:** Python 3.14, pandas, numpy, PyYAML, pytest. 설계 근거: `src/agent_b_spc/specs/B2-1_baseline재생성_설계.md`.

## Global Constraints

- **헌법 6-1**: 하드코딩 매직넘버 금지. 파라미터 단일 소스 = `config/params.yaml`. 키 부재 시 매직넘버 대체 없이 **누락 키 이름을 담은 명시적 에러**.
- **헌법 6-4**: `snake_case`. 코드·DB·CSV에서 센서는 **C코드**(`C11` 등) 그대로 — 발표용 이름 하드코딩 금지.
- **분위수 레벨 고정**: `q_low=0.00135`, `q_high=0.99865` (±3σ 등가). config `spc` 섹션 등재(B 권한).
- **분위수 경계는 트림 전 `values`에서 산출** — 트림된 표본이면 경계가 절단선에 붙어 붕괴(오탐 ~2%). 트림은 σ-track 전용.
- **커밋 경계**: Stage A(config 이관)는 반드시 diff=0. `MIN_TRIM_N`·`MIN_GROUP_N` 가드 제거는 **반드시 C6_1 제외와 같은 커밋(Task 4)** — 커밋 A에서 제거하면 C6_1 그룹(n=33~93<100)이 트림돼 diff≠0.
- **가드 제거는 n≥100 사전 스캔 게이트 통과 시에만** — 미달 그룹 있으면 **하드 실패(`RuntimeError`)로 중단**(소표본 조용히 트림 금지). 현 데이터는 전 그룹 ≥500이라 무발동.
- **브랜치**: `feat/agent-b-baseline-regen` (이미 체크아웃됨, PR #20 sync 위). baseline PR은 #20 머지 후 main 대상.
- **총 그룹 수 기대값 = 77** (148 −74 C6_1 −5 D_CH5960 −2 C54/56 과도 +10 C54/56 정착).
- **테스트**: 현 47 tests는 전부 엔진 테스트 → 이 두 파일 커버리지 0. 신규 유닛테스트를 추가하고 기존 47 회귀 통과 유지.

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `config/params.yaml` | 파라미터 단일 소스 | `spc`에 `q_low`/`q_high` 등재 |
| `src/agent_b_spc/initial_limits.py` | 초기 실력치 산출 (σ + 분위수 2-track) | 4결정·config 로드/주입·죽은코드 제거 |
| `src/agent_b_spc/monitoring_whitelist.py` | 감시 판정 + `method` 부여 | config 로드/주입·`method` 부여 |
| `src/agent_b_spc/analysis/false_alarm_mechanism.py` | KEEP\* 오탐 근거 스크립트 | 복귀 블록(최근 500) 기준으로 갱신 |
| `src/agent_b_spc/tests/test_initial_limits.py` | (신규) initial_limits 유닛테스트 | 생성 |
| `src/agent_b_spc/tests/test_monitoring_whitelist.py` | (신규) whitelist 유닛테스트 | 생성 |
| `src/agent_b_spc/limits/*.csv`·`*_meta.json`·`qa_report_v1.md`·`monitoring_whitelist_v1.md` | 재생성 산출물 | Task 8에서 재생성 |
| `src/agent_b_spc/README.md`·`회의안건_요약_B.md` | 문서 | Task 9 |

---

# Stage A — config 이관 (출력 불변, diff=0)

### Task 1: initial_limits config 이관 (`LimitConfig` 주입)

module-top-level `_LIMIT_CFG` 즉시 로드를 호출 시점 로드로 바꾸고, 이관 3키(`trim_quantile`·`false_alarm_warn_pct`·`cv_warn`)를 config에서 읽어 `compute_limits`에 주입한다. 출력은 불변(값 동일).

**Files:**
- Modify: `src/agent_b_spc/initial_limits.py`
- Test: `src/agent_b_spc/tests/test_initial_limits.py` (create)

**Interfaces:**
- Produces: `LimitConfig` (frozen dataclass, Stage A 필드 6종), `LimitConfig.load(path) -> LimitConfig`, `compute_limits(summaries: pd.DataFrame, cfg: LimitConfig) -> pd.DataFrame`, `determine_sample_wafers(df, cfg)`.

- [ ] **Step 1: 실패 테스트 작성 — config 로더**

`src/agent_b_spc/tests/test_initial_limits.py` 생성:

```python
"""initial_limits 유닛테스트 (config 로드·주입·2-track·게이트)."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from src.agent_b_spc import initial_limits as il


def _write_cfg(tmp_path: Path, spc: dict | None = None, limit_engine: dict | None = None) -> Path:
    """테스트용 최소 params.yaml 작성 (hermetic)."""
    cfg = {
        "limit_engine": {
            "control_limit_k_sigma": 3.0,
            "rolling_window_n": 500,
            "seasoning_exclude_wafers": 10,
            **(limit_engine or {}),
        },
        "spc": {
            "trim_quantile": 0.01,
            "false_alarm_warn_pct": 2.0,
            "cv_warn": 0.5,
            **(spc or {}),
        },
    }
    p = tmp_path / "params.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def test_limitconfig_load_reads_both_sections(tmp_path):
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    assert cfg.k_sigma == 3.0
    assert cfg.rolling_n == 500
    assert cfg.trim_quantile == 0.01
    assert cfg.false_alarm_warn_pct == 2.0
    assert cfg.cv_warn == 0.5


def test_limitconfig_missing_key_names_the_key(tmp_path):
    p = _write_cfg(tmp_path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    del data["spc"]["cv_warn"]
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(KeyError, match="cv_warn"):
        il.LimitConfig.load(p)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_initial_limits.py -v`
Expected: FAIL — `AttributeError: module 'initial_limits' has no attribute 'LimitConfig'`

- [ ] **Step 3: `LimitConfig` + 헬퍼 구현**

`initial_limits.py` 상단, 기존 `load_limit_config`/`_LIMIT_CFG`/개별 상수(`K_SIGMA`~`CV_WARN`) 블록(현재 34~64행)을 아래로 교체. `import`에 `from dataclasses import dataclass` 추가.

```python
def _section(cfg: dict, name: str, path) -> dict:
    """params.yaml의 최상위 섹션을 반환. 부재 시 명시적 실패 (헌법 6-1)."""
    sec = cfg.get(name)
    if not sec:
        raise RuntimeError(f"config에 [{name}] 섹션이 없습니다: {path}")
    return sec


def _key(sec: dict, key: str, section: str, path):
    """섹션에서 키를 꺼낸다. 부재 시 누락 키 이름을 담아 실패 (헌법 6-1)."""
    if key not in sec:
        raise KeyError(f"config [{section}]에 '{key}' 키가 없습니다: {path}")
    return sec[key]


@dataclass(frozen=True)
class LimitConfig:
    """initial_limits 확정 파라미터 (params.yaml 단일 소스). Stage B에서 q_low/q_high 추가."""
    k_sigma: float
    rolling_n: int
    seasoning_wafers: int
    trim_quantile: float
    false_alarm_warn_pct: float
    cv_warn: float

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "LimitConfig":
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        le = _section(cfg, "limit_engine", path)
        spc = _section(cfg, "spc", path)
        return cls(
            k_sigma=float(_key(le, "control_limit_k_sigma", "limit_engine", path)),
            rolling_n=int(_key(le, "rolling_window_n", "limit_engine", path)),
            seasoning_wafers=int(_key(le, "seasoning_exclude_wafers", "limit_engine", path)),
            trim_quantile=float(_key(spc, "trim_quantile", "spc", path)),
            false_alarm_warn_pct=float(_key(spc, "false_alarm_warn_pct", "spc", path)),
            cv_warn=float(_key(spc, "cv_warn", "spc", path)),
        )


# 죽은 코드 아님 — Stage B(Task 4)에서 제거. Stage A는 기존 동작 유지.
MIN_TRIM_N = 100
MIN_GROUP_N = 30
INITIAL_LIMIT_VERSION = "v1"
```

> `SENSORS_SETTLED`·`SENSORS_TRANSIENT_ONLY`·`DERIVED_SETTLED`·`ID_COLS`는 그대로 둔다(Task 4·5에서 변경).

- [ ] **Step 4: `compute_limits`·`determine_sample_wafers`에 `cfg` 주입**

`compute_limits(summaries)` → `compute_limits(summaries, cfg: LimitConfig)`. 본문의 `K_SIGMA`→`cfg.k_sigma`, `ROLLING_N`→`cfg.rolling_n`, `TRIM_QUANTILE`→`cfg.trim_quantile`, `FALSE_ALARM_WARN_PCT`→`cfg.false_alarm_warn_pct`, `CV_WARN`→`cfg.cv_warn`로 교체. `MIN_TRIM_N`·`MIN_GROUP_N`은 Stage A에선 **그대로 사용**(모듈 상수). `determine_sample_wafers(df)` → `determine_sample_wafers(df, cfg)`, 본문 `SEASONING_WAFERS`→`cfg.seasoning_wafers`.

- [ ] **Step 5: `main()`에서 cfg 로드·전달**

`main()` 본문 시작에 `cfg = LimitConfig.load()` 추가, `determine_sample_wafers(df, cfg)`·`compute_limits(summaries, cfg)` 호출로 교체. `meta` 저장부의 `K_SIGMA`/`ROLLING_N`/`TRIM_QUANTILE`도 `cfg.*`로 교체.

- [ ] **Step 6: 로더 테스트 통과 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_initial_limits.py -v`
Expected: PASS (2 passed)

- [ ] **Step 7: diff=0 증명 — 재생성 후 데이터 컬럼 동일**

Run:
```bash
python -m src.agent_b_spc.initial_limits --out-dir "$TMPDIR/regenA"
python - <<'PY'
import os, pandas as pd
from pandas.testing import assert_frame_equal
old = pd.read_csv("src/agent_b_spc/limits/initial_limits_v1.csv").drop(columns=["created_at"])
new = pd.read_csv(os.path.join(os.environ["TMPDIR"], "regenA", "initial_limits_v1.csv")).drop(columns=["created_at"])
assert_frame_equal(old, new, check_exact=False, rtol=1e-9)
print("diff=0 OK (created_at 제외 데이터 동일)")
PY
```
Expected: `diff=0 OK` (config 이관은 출력 불변). 실패 시 값 불일치 — 이관 매핑 오류.

- [ ] **Step 8: 기존 회귀 + 커밋**

Run: `python -m pytest src/agent_b_spc/tests/ -q`
Expected: 49 passed (기존 47 + 신규 2)

```bash
git add src/agent_b_spc/initial_limits.py src/agent_b_spc/tests/test_initial_limits.py
git commit -m "refactor: initial_limits config 이관(LimitConfig 주입) — 출력 불변 diff=0"
```

---

### Task 2: monitoring_whitelist config 이관 (`WhitelistConfig` 주입)

`ACTIVITY_MIN`·`MAG_MIN`·`FALSE_ALARM_WARN_PCT` 상수를 config(`spc`)에서 읽어 `classify`에 주입. 출력 불변.

**Files:**
- Modify: `src/agent_b_spc/monitoring_whitelist.py`
- Test: `src/agent_b_spc/tests/test_monitoring_whitelist.py` (create)

**Interfaces:**
- Consumes: 없음 (독립).
- Produces: `WhitelistConfig` (frozen dataclass), `WhitelistConfig.load(path)`, `classify(limits: pd.DataFrame, cfg: WhitelistConfig) -> pd.DataFrame`.

- [ ] **Step 1: 실패 테스트 작성**

`src/agent_b_spc/tests/test_monitoring_whitelist.py` 생성:

```python
"""monitoring_whitelist 유닛테스트 (config·decision·method)."""
from pathlib import Path

import pandas as pd
import pytest
import yaml

from src.agent_b_spc import monitoring_whitelist as mw


def _write_cfg(tmp_path: Path, spc: dict | None = None) -> Path:
    cfg = {"spc": {"activity_min": 0.05, "mag_min": 0.10, "false_alarm_warn_pct": 2.0, **(spc or {})}}
    p = tmp_path / "params.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def test_whitelistconfig_load(tmp_path):
    cfg = mw.WhitelistConfig.load(_write_cfg(tmp_path))
    assert cfg.activity_min == 0.05
    assert cfg.mag_min == 0.10
    assert cfg.false_alarm_warn_pct == 2.0


def test_whitelistconfig_missing_key_names_it(tmp_path):
    p = _write_cfg(tmp_path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    del data["spc"]["mag_min"]
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(KeyError, match="mag_min"):
        mw.WhitelistConfig.load(p)
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_monitoring_whitelist.py -v`
Expected: FAIL — `AttributeError: ... 'WhitelistConfig'`

- [ ] **Step 3: `WhitelistConfig` 구현**

`monitoring_whitelist.py` 상단 상수 3종(`ACTIVITY_MIN`·`MAG_MIN`·`FALSE_ALARM_WARN_PCT`, 현재 43~45행)을 교체. `import`에 `from dataclasses import dataclass`, `import yaml` 추가(**`from pathlib import Path`는 현재 35행에 이미 있음** — 추가 불필요)하고 `CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"` 정의. `_section`·`_key`는 `initial_limits`에서 재사용 — **절대 임포트**(코드베이스 컨벤션, `nelson_engine.py` 정합): `from src.agent_b_spc.initial_limits import _section, _key`.

```python
@dataclass(frozen=True)
class WhitelistConfig:
    """whitelist 판정 임계 (params.yaml `spc` 단일 소스)."""
    activity_min: float
    mag_min: float
    false_alarm_warn_pct: float

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "WhitelistConfig":
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        spc = _section(cfg, "spc", path)
        return cls(
            activity_min=float(_key(spc, "activity_min", "spc", path)),
            mag_min=float(_key(spc, "mag_min", "spc", path)),
            false_alarm_warn_pct=float(_key(spc, "false_alarm_warn_pct", "spc", path)),
        )
```

- [ ] **Step 4: `classify`에 `cfg` 주입**

`classify(limits)` → `classify(limits, cfg: WhitelistConfig)`. 내부 `decide` 클로저의 `ACTIVITY_MIN`→`cfg.activity_min`, `MAG_MIN`→`cfg.mag_min`, `FALSE_ALARM_WARN_PCT`→`cfg.false_alarm_warn_pct`. `main()`에 `cfg = WhitelistConfig.load()` 추가, `classify(limits, cfg)` 호출.

- [ ] **Step 5: 테스트 통과 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_monitoring_whitelist.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: diff=0 증명**

Run:
```bash
python -m src.agent_b_spc.monitoring_whitelist --out-dir "$TMPDIR/regenA"
python - <<'PY'
import os, pandas as pd
from pandas.testing import assert_frame_equal
old = pd.read_csv("src/agent_b_spc/limits/monitoring_whitelist_v1.csv")
new = pd.read_csv(os.path.join(os.environ["TMPDIR"], "regenA", "monitoring_whitelist_v1.csv"))
assert_frame_equal(old, new, check_exact=False, rtol=1e-9)
print("whitelist diff=0 OK")
PY
```
Expected: `whitelist diff=0 OK`

- [ ] **Step 7: 커밋**

Run: `python -m pytest src/agent_b_spc/tests/ -q`
Expected: 51 passed

```bash
git add src/agent_b_spc/monitoring_whitelist.py src/agent_b_spc/tests/test_monitoring_whitelist.py
git commit -m "refactor: monitoring_whitelist config 이관(WhitelistConfig 주입) — 출력 불변 diff=0"
```

> **Stage A 완료 체크포인트**: 여기까지 두 산출물 diff=0. 이후 Task부터 기대 diff 발생.

---

# Stage B — 4결정 + 죽은 코드 제거 + q 등재 (기대 diff)

### Task 3: q_low/q_high config 등재 + `LimitConfig` 확장

조합②의 분위수 레벨을 config에 등재(B 권한, 회의 "구현 착수 시 등재" 승인)하고 `LimitConfig`에 필드 추가. 아직 사용 안 함(출력 불변).

**Files:**
- Modify: `config/params.yaml`, `src/agent_b_spc/initial_limits.py`
- Test: `src/agent_b_spc/tests/test_initial_limits.py`

- [ ] **Step 1: 실패 테스트 추가**

`test_initial_limits.py`의 `_write_cfg` spc 기본값에 `"q_low": 0.00135, "q_high": 0.99865` 추가하고 아래 테스트 추가:

```python
def test_limitconfig_loads_quantile_levels(tmp_path):
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    assert cfg.q_low == 0.00135
    assert cfg.q_high == 0.99865
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_initial_limits.py::test_limitconfig_loads_quantile_levels -v`
Expected: FAIL — `TypeError: __init__() ... unexpected ... q_low` 또는 `AttributeError`

- [ ] **Step 3: params.yaml 등재**

`config/params.yaml`의 `spc:` 섹션, `cv_warn` 다음 줄에 추가:

```yaml
  q_low: 0.00135                        # 조합② 분위수 하한 레벨 (±3σ 등가 0.135% — KEEP* 분위수 관리선, B 등재 2026-07-12)
  q_high: 0.99865                       # 조합② 분위수 상한 레벨 (±3σ 등가 99.865%)
```

- [ ] **Step 4: `LimitConfig`에 필드 추가**

`LimitConfig`에 `q_low: float`, `q_high: float` 필드 추가, `load()`에 두 줄 추가:

```python
    q_low: float
    q_high: float
```
```python
            q_low=float(_key(spc, "q_low", "spc", path)),
            q_high=float(_key(spc, "q_high", "spc", path)),
```

- [ ] **Step 5: 통과 + 커밋**

Run: `python -m pytest src/agent_b_spc/tests/test_initial_limits.py -v`
Expected: PASS

```bash
git add config/params.yaml src/agent_b_spc/initial_limits.py src/agent_b_spc/tests/test_initial_limits.py
git commit -m "feat: 조합② 분위수 레벨 q_low/q_high config 등재 + LimitConfig 확장 (B 권한, 회의 승인)"
```

---

### Task 4: C6_1 제외(복귀 블록) + 게이트 + 죽은 코드 제거 (한 커밋)

`determine_sample_wafers`를 recipe-aware로 바꿔 baseline을 **복귀 블록(마지막 C6_1 이후 연속 C6_0)**으로 정의하고, `excluded_experimental`를 meta에 기록. 동시에 `MIN_TRIM_N`·`MIN_GROUP_N` 가드를 제거(n≥100 게이트 통과 시). **가드 제거는 반드시 이 커밋**(C6_1이 있으면 트림돼 diff≠0).

**Files:**
- Modify: `src/agent_b_spc/initial_limits.py`
- Test: `src/agent_b_spc/tests/test_initial_limits.py`

**Interfaces:**
- Produces: `determine_sample_wafers(df, cfg) -> (list[str], dict)` — meta에 `excluded_experimental` 포함. `compute_limits`는 `qa_flags`에서 `low_n` 미생성(제거), 트림 항상 적용.

- [ ] **Step 1: 실패 테스트 — 복귀 블록 필터 + excluded_experimental**

```python
def _synthetic_reset_df():
    """PM 리셋 후 C6_0(전)→C6_1(실험)→C6_0(복귀) 3블록 합성."""
    rows = []
    wid = 0
    def block(recipe, n, t0, c33):
        nonlocal wid
        for i in range(n):
            wid += 1
            # 각 wafer 1 step(4)·settled 1점 — 값은 recipe로 구분
            rows.append(dict(C64=f"W{wid:04d}", C6=recipe, C7=4, C42=0, C24="C24_0",
                             C10=t0 + wid, C33=c33, C11=-220.0, C12=-5.0, C59=50.0, C60=60.0,
                             C15=1.0, C16=1.0, C17=50.0, C9=50.0, C31=100.0, C32=1.0,
                             C57=1.0, C58=1.0, C61=-10.0, C62=100.0, C63=5.0,
                             C18=1.0, C27=1.0, C54=1.0, C56=1.0, C20=0, C6_dummy=0))
    block("C6_0", 20, 1_000, c33=5)     # 실험 前 (리셋 직후 → c33 상승 지점)
    block("C6_1", 5, 2_000, c33=5)      # 실험
    block("C6_0", 600, 3_000, c33=5)    # 복귀
    df = pd.DataFrame(rows)
    # 리셋 지점 생성: 첫 블록 앞에 c33 큰 wafer 하나
    head = df.iloc[:1].copy(); head["C64"] = "W0000"; head["C33"] = 99; head["C10"] = 0
    return pd.concat([head, df], ignore_index=True)


def test_sample_is_return_block_only(tmp_path):
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    df = _synthetic_reset_df()
    sample, meta = il.determine_sample_wafers(df, cfg)
    sample_recipes = df[df.C64.isin(sample)].groupby("C64")["C6"].first()
    assert (sample_recipes == "C6_0").all()                 # C6_1 없음
    # 복귀 블록만 (실험 前 20장 제외)
    assert meta["excluded_experimental"]["C6_1"]["n"] == 5
    assert meta["excluded_experimental"]["C6_0_pre_experiment"]["n"] == 20
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_initial_limits.py::test_sample_is_return_block_only -v`
Expected: FAIL — 기존 `determine_sample_wafers`는 recipe 무시(전 세그먼트 반환), `excluded_experimental` 없음

- [ ] **Step 3: `determine_sample_wafers` 복귀 블록화**

기존 함수를 교체:

```python
def determine_sample_wafers(df: pd.DataFrame, cfg: LimitConfig) -> tuple[list[str], dict]:
    """표본 구간 확정 (A8+A7 + R1 복귀 블록).

    마지막 PM 리셋(C33 증가) 세그먼트에서, C6_1 실험 블록 **이후**의 연속 C6_0(복귀 블록)만
    표본으로 쓴다. 실험 前 C6_0는 '현재 레짐 창 밖'으로 제외(갭 가로지름 방지 — 설계 R1).
    seasoning(A7)은 복귀 블록엔 post-PM wafer가 없어 의미상 N/A이나 코드 일관성 위해 유지(무영향).
    """
    w = (df.groupby("C64")
         .agg(t0=("C10", "min"), c33=("C33", "max"), recipe=("C6", "first"))
         .sort_values("t0").reset_index())
    reset_idx = w.index[w["c33"] < w["c33"].shift(1)].tolist()
    if not reset_idx:
        raise ValueError("C33 리셋 지점을 찾지 못했습니다 — 표본 구간(A8)을 확정할 수 없습니다.")
    last_reset = reset_idx[-1]
    segment = w.iloc[last_reset:]

    exp = segment[segment["recipe"] == "C6_1"]
    if not exp.empty:
        last_exp_t0 = exp["t0"].max()
        return_block = segment[(segment["recipe"] == "C6_0") & (segment["t0"] > last_exp_t0)]
        pre_exp = segment[(segment["recipe"] == "C6_0") & (segment["t0"] < exp["t0"].min())]
    else:
        return_block = segment[segment["recipe"] == "C6_0"]
        pre_exp = segment.iloc[0:0]

    sample = return_block.iloc[cfg.seasoning_wafers:]

    def _period(s):
        if s.empty:
            return [None, None]
        return [str(pd.to_datetime(s["t0"].min(), unit="s")),
                str(pd.to_datetime(s["t0"].max(), unit="s"))]

    meta = {
        "reset_wafer": str(w.loc[last_reset, "C64"]),
        "reset_position": int(last_reset),
        "segment_wafers": int(len(segment)),
        "seasoning_excluded": cfg.seasoning_wafers,
        "sample_wafers": int(len(sample)),
        "sample_period": _period(sample),
        "excluded_experimental": {
            "C6_1": {"n": int(len(exp)), "period": _period(exp)},
            "C6_0_pre_experiment": {"n": int(len(pre_exp)), "period": _period(pre_exp),
                                    "reason": "현재 레짐 창 밖(실험 前 생산)"},
        },
    }
    logger.info("표본(복귀 블록): C6_0 %d장 − seasoning %d = %d장 / 제외 C6_1 %d·실험前 C6_0 %d",
                len(return_block), cfg.seasoning_wafers, len(sample), len(exp), len(pre_exp))
    return sample["C64"].tolist(), meta
```

- [ ] **Step 4: `compute_limits` 죽은 코드 제거 (n≥100 게이트)**

`compute_limits`에서 `if n_raw >= MIN_TRIM_N: ... else: trimmed = values` 분기를 **항상 트림**으로 교체하고, `low_n` 플래그 생성 줄을 제거. **루프 진입 전 사전 스캔 게이트**를 하드 실패로 둔다(설계 §5 — 미달 시 하드 실패 중단). **임계값 100은 하드코딩하지 않고 config `min_trim_n`을 로드해 재사용**(헌법 6-1 — 트림 가드에서 제거하되 게이트 임계로 살림). t0 연속성 게이트(§5 ②)는 R1 복귀블록 필터가 갭을 **구조적으로 차단**하므로 별도 미구현(spec §5 ② redundant).

> **`LimitConfig` 확장**: `min_trim_n: int` 필드 + `load()`에 `min_trim_n=int(_key(spc, "min_trim_n", "spc", path))` 한 줄. 테스트 `_write_cfg` spc 기본값에 `"min_trim_n": 100` 추가(누락 시 fail-fast).

```python
    group_cols = ["C24", "C6", "C7", "sensor_window", "sensor_id"]

    # 선검증 게이트 ① (설계 §5): 전 그룹 n≥cfg.min_trim_n — 미달 시 하드 실패 중단.
    #   소표본을 조용히 트림해 왜곡하지 않는다. 현 데이터는 전 그룹 ≥500이라 무발동(미래 안전망).
    #   임계는 config min_trim_n 재사용(헌법 6-1). 게이트 ② t0 연속성은 R1 필터로 구조 보장 → 미구현(§5 ② redundant).
    sizes = summaries.groupby(group_cols).size()
    small = sizes[sizes < cfg.min_trim_n]
    if len(small):
        raise RuntimeError(
            f"n<{cfg.min_trim_n} 그룹 {len(small)}건 — 트림 항상-적용 가정 위반, 재검토 필요: {small.to_dict()}")

    rows = []
    for keys, g in summaries.groupby(group_cols):
        values = g.sort_values("t0")["value"].to_numpy()[-cfg.rolling_n:]
        n_raw = len(values)
        lo, hi = np.quantile(values, [cfg.trim_quantile, 1 - cfg.trim_quantile])
        trimmed = values[(values >= lo) & (values <= hi)]      # 트림은 σ-track 전용
        mu = float(np.mean(trimmed))
        sigma = float(np.std(trimmed, ddof=1)) if len(trimmed) > 1 else 0.0
        ...
        flags = []
        # low_n 제거 (MIN_GROUP_N 죽은 코드) — 소표본 가드는 B4-3
        if sigma == 0.0 or not np.isfinite(sigma):
            flags.append("zero_sigma")
        ...
```

이어서 모듈 상단 `MIN_TRIM_N = 100`·`MIN_GROUP_N = 30` 상수 2줄 삭제(값 100은 `cfg.min_trim_n`으로 이동, `MIN_GROUP_N`은 완전 폐기). `compute_limits` docstring의 `MIN_TRIM_N` 언급도 정리.

> **테스트 보강**: Step 5의 `test_compute_limits_always_trims`에 더해, **n<100 그룹이 있으면 `RuntimeError`가 나는지** 검증하는 테스트를 추가한다:
> ```python
> def test_small_group_raises(tmp_path):
>     cfg = il.LimitConfig.load(_write_cfg(tmp_path))
>     summ = pd.DataFrame({"C24":"C24_0","C6":"C6_0","C7":4,"sensor_window":"settled",
>         "sensor_id":"C11","C64":[f"W{i}" for i in range(50)],"t0":range(50),"value":[10.0]*50})
>     with pytest.raises(RuntimeError, match="n<100"):
>         il.compute_limits(summ, cfg)
> ```
> (Step 5의 `test_compute_limits_always_trims`는 게이트를 통과하도록 **n=150**으로 둔다 — n<100이면 위 게이트에 걸려 `RuntimeError`가 나므로.)

- [ ] **Step 5: 트림 항상-적용 테스트**

```python
def test_compute_limits_always_trims(tmp_path):
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    # n=150(게이트 통과) — 극단치 2개가 트림돼 σ 작아지고 low_n 플래그 없음
    vals = [10.0] * 148 + [1000.0, -1000.0]
    summ = pd.DataFrame({
        "C24": "C24_0", "C6": "C6_0", "C7": 4, "sensor_window": "settled",
        "sensor_id": "C11", "C64": [f"W{i}" for i in range(150)],
        "t0": range(150), "value": vals,
    })
    out = il.compute_limits(summ, cfg)
    row = out.iloc[0]
    assert row["sigma"] < 50            # 극단치 트림됨 → σ 작음
    assert "low_n" not in row["qa_flags"]   # low_n 플래그 제거됨
```

- [ ] **Step 6: 테스트 통과 + main 정합**

`main()`은 원래부터 `sample_wafers, meta = determine_sample_wafers(...)`로 **튜플 언패킹**(원함수가 이미 `return ..., meta`)이며, Task 1에서 `cfg` 인자만 추가됨. Task 4는 반환 arity를 바꾸지 않고(여전히 `(list, dict)`) `meta`만 `excluded_experimental`로 확장 → main 무변경. `meta` 저장 시 `initial_limits_v1_meta.json`에 `excluded_experimental`가 실리는지 확인. Run:
`python -m pytest src/agent_b_spc/tests/test_initial_limits.py -v`
Expected: PASS (전체)

- [ ] **Step 7: 커밋 (가드 제거 + C6_1 제외 한 커밋)**

```bash
git add src/agent_b_spc/initial_limits.py src/agent_b_spc/tests/test_initial_limits.py
git commit -m "feat: C6_1 실험구간 baseline 제외(복귀 블록) + MIN_TRIM_N/MIN_GROUP_N 가드 제거(한 커밋, no-op 경계)"
```

---

### Task 5: C54·C56 정착 이동 + D_CH5960 제외

센서 목록 상수만 변경 + 파생 계산·로드 정리.

> **실측 결과 (정직 기록 — 리뷰 R6차)**: C54/C56을 정착으로 옮기면 **steps 1·5·6·7=상수0(zero_sigma→EXCLUDE) +
> step4=KEEP(광폭 ±3σ, self-FA 0%)** = 8 EXCLUDE + 2 KEEP. step4 KEEP이나 밴드가 광폭이라 **사실상 무력**.
> 이 순효과(과도 감시 제거 + 정착 광폭 무력 = C54/C56 실질 감시 약화)는 **W7 백로그·팀 재검토**(스펙 §3-2).
> 이 Task는 회의 결정(정착 이동)을 그대로 구현하고 결과를 DoD·meta에 정직하게 반영할 뿐, EXCLUDE로 강제하지 않는다.

**Files:**
- Modify: `src/agent_b_spc/initial_limits.py`
- Test: `src/agent_b_spc/tests/test_initial_limits.py`

- [ ] **Step 1: 실패 테스트 — 센서 분류·파생**

```python
def test_sensor_lists_reflect_decisions():
    assert "C54" in il.SENSORS_SETTLED and "C56" in il.SENSORS_SETTLED
    assert "C54" not in il.SENSORS_TRANSIENT_ONLY and "C56" not in il.SENSORS_TRANSIENT_ONLY
    assert il.SENSORS_TRANSIENT_ONLY == ["C18", "C27"]
    assert "D_CH5960" not in il.DERIVED_SETTLED
    assert il.DERIVED_SETTLED == ["D_VDC_RES"]


def test_derived_features_no_ch5960():
    df = pd.DataFrame({"C11": [-220.0], "C12": [-5.0]})
    out = il.add_derived_features(df)
    assert "D_VDC_RES" in out.columns
    assert "D_CH5960" not in out.columns
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_initial_limits.py::test_sensor_lists_reflect_decisions src/agent_b_spc/tests/test_initial_limits.py::test_derived_features_no_ch5960 -v`
Expected: FAIL

- [ ] **Step 3: 센서 목록·파생·로드 수정**

`initial_limits.py`에서:
- `SENSORS_SETTLED` 리스트 끝에 `"C54"`, `"C56"` 추가 (C코드 주석 병기: `# C54 Match 축 위치(정착 준고정)`).
- `SENSORS_TRANSIENT_ONLY = ["C18", "C27", "C54", "C56"]` → `["C18", "C27"]`.
- `DERIVED_SETTLED = ["D_VDC_RES", "D_CH5960"]` → `["D_VDC_RES"]`.
- `add_derived_features`에서 `df["D_CH5960"] = np.where(...)` 줄 삭제 (D_VDC_RES만 유지).
- `load_data`의 `usecols`에서 `"C59", "C60"` 제거 (`"C12"`는 D_VDC_RES용 유지):
  `usecols = sorted(set(ID_COLS + SENSORS_SETTLED + SENSORS_TRANSIENT_ONLY + ["C12"]))`

- [ ] **Step 4: 통과 + 커밋**

Run: `python -m pytest src/agent_b_spc/tests/test_initial_limits.py -v`
Expected: PASS

```bash
git add src/agent_b_spc/initial_limits.py src/agent_b_spc/tests/test_initial_limits.py
git commit -m "feat: C54·C56 정착 이동 + D_CH5960 SPC 제외 (센서 목록·파생·로드 정리)"
```

---

### Task 6: 조합② — 분위수 컬럼(트림 전) + whitelist `method`

`compute_limits`가 전 그룹에 분위수 레벨·경계값을 산출(트림 전 `values`)하고, whitelist가 KEEP\*에 `method=quantile`을 부여.

**Files:**
- Modify: `src/agent_b_spc/initial_limits.py`, `src/agent_b_spc/monitoring_whitelist.py`
- Test: 양쪽 테스트 파일

**Interfaces:**
- Produces: `initial_limits_v1.csv`에 `q_low`·`q_high`·`q_lcl`·`q_ucl` 컬럼. `monitoring_whitelist_v1.csv`에 `method` 컬럼(`quantile`/`sigma`).

- [ ] **Step 1: 실패 테스트 — 분위수는 트림 전에서 산출**

```python
def test_quantile_bounds_from_pretrim_values(tmp_path):
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    # 최솟값이 극단 — 트림 후엔 사라지지만 분위수(트림 전)엔 반영돼야
    vals = [-9999.0] + [10.0] * 498 + [9999.0]     # n=500
    summ = pd.DataFrame({
        "C24": "C24_0", "C6": "C6_0", "C7": 4, "sensor_window": "settled",
        "sensor_id": "C11", "C64": [f"W{i}" for i in range(500)],
        "t0": range(500), "value": vals,
    })
    row = il.compute_limits(summ, cfg).iloc[0]
    assert row["q_low"] == 0.00135 and row["q_high"] == 0.99865
    # 트림 전 분위수라 경계가 극단쪽으로 넓음 (트림된 표본이면 ±10 근처로 붕괴)
    assert row["q_lcl"] < -100 and row["q_ucl"] > 100
    # σ는 트림 후라 좁음 (분리 확인)
    assert abs(row["sigma"]) < 50
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest src/agent_b_spc/tests/test_initial_limits.py::test_quantile_bounds_from_pretrim_values -v`
Expected: FAIL — `KeyError: 'q_low'`

- [ ] **Step 3: `compute_limits`에 분위수 컬럼 추가**

트림 계산 직후, σ 산출과 **분리**해 트림 전 `values`에서 분위수 경계 산출. row dict에 4컬럼 추가:

```python
        # 분위수 관리선 (조합②) — 반드시 트림 전 values에서 (트림된 표본이면 경계 붕괴)
        q_lcl = float(np.quantile(values, cfg.q_low))
        q_ucl = float(np.quantile(values, cfg.q_high))
```
```python
        rows.append(
            dict(zip(["chamber_id", "recipe_id", "step", "sensor_window", "sensor_id"], keys))
            | {
                ...
                "k_sigma": cfg.k_sigma,
                "q_low": cfg.q_low,
                "q_high": cfg.q_high,
                "q_lcl": q_lcl,
                "q_ucl": q_ucl,
            }
        )
```

- [ ] **Step 4: whitelist `method` 컬럼 실패 테스트**

`test_monitoring_whitelist.py`에 추가:

```python
def _limits_row(**kw):
    base = dict(chamber_id="C24_0", recipe_id="C6_0", step=4, sensor_window="settled",
                sensor_id="C11", sigma=1.0, center=0.0, sample_min=-3.0, sample_max=3.0,
                false_alarm_pct=0.0, qa_flags="", cv=0.1, n_distinct=100)
    base.update(kw); return base


def test_method_quantile_for_keepstar(tmp_path):
    cfg = mw.WhitelistConfig.load(_write_cfg(tmp_path))
    limits = pd.DataFrame([
        _limits_row(sensor_id="C11", false_alarm_pct=0.0),      # KEEP → sigma
        _limits_row(sensor_id="C61", false_alarm_pct=5.0),      # KEEP* → quantile
    ])
    out = mw.classify(limits, cfg)
    m = dict(zip(out["sensor_id"], out["method"]))
    assert m["C11"] == "sigma"
    assert m["C61"] == "quantile"


def test_constant_sensor_excluded(tmp_path):
    """상수(σ=0/range=0) 센서 → EXCLUDE. Task 8 DoD가 C54/C56 step1/5/6/7에 hard-assert하는 경로."""
    cfg = mw.WhitelistConfig.load(_write_cfg(tmp_path))
    limits = pd.DataFrame([_limits_row(sensor_id="C54", step=1, sigma=0.0,
                                        center=0.0, sample_min=0.0, sample_max=0.0)])
    out = mw.classify(limits, cfg)
    assert out.iloc[0]["decision"] == "EXCLUDE"
```
> `_limits_row`에 `range=sample_max-sample_min`이 없으면 `classify`가 `range`를 `sample_max-sample_min`로 계산하므로(현행 코드) 위 상수 행은 `range=0`·`sigma=0` → decide()의 `EXCLUDE` 분기에 걸린다.

- [ ] **Step 5: whitelist `method` 부여 구현**

`classify`에서 **`apply_overrides` 이후**(최종 decision 기준)에 `method` 컬럼 부여 — 오버라이드가 KEEP\*를 EXCLUDE로 바꿔도 정합(현재 오버라이드는 D_VDC_RES→EXCLUDE 하나뿐이나 견고성). `classify` 말미 `return apply_overrides(df)`를 아래로 교체:

```python
    df = apply_overrides(df)
    df["method"] = np.where(df["decision"] == "KEEP*", "quantile", "sigma")
    return df
```
`import numpy as np` 추가. `main()`의 출력 `cols` 리스트에 `"method"` 추가(`decision` 뒤).

- [ ] **Step 6: 통과 + 커밋**

Run: `python -m pytest src/agent_b_spc/tests/ -q`
Expected: 전체 PASS

```bash
git add src/agent_b_spc/initial_limits.py src/agent_b_spc/monitoring_whitelist.py src/agent_b_spc/tests/
git commit -m "feat: 조합② 2-track — 분위수 컬럼(트림 전 산출) + whitelist method 부여"
```

---

### Task 7: (제거됨 — 리뷰 R6차)

> **modality 게이트 폐기.** 초안은 "C54/C56 step4 정착 = bimodal → EXCLUDE"를 전제했으나, `is_bimodal_crossing_zero`
> (band 0.10·valley 0.15)를 실데이터에 돌리면 **`bimodal=False`**(center_density C54-s4 25.4%·C56-s4 29.0% > 15%).
> step4는 깨끗한 이봉이 아니라 광범위 다봉/skew라 게이트가 **타깃을 못 잡는 dead code**이고, whitelist 판정은
> **KEEP**(광폭 ±3σ, self-FA 0%)이 회의 결정("정착 신호 감시")·데이터와 정합. valley 임계를 올리면 정상 광폭까지
> 오탐. → **게이트·config 2키·함수 전부 미도입.** "광폭 밴드 무력" 문제는 별개 도메인 판단으로 **W7 백로그**(스펙 §3-2).
> Task 번호는 유지(8·9)한다.

---

### Task 8: 재생성 + DoD 검증 + 근거 스크립트 갱신

전 산출물을 재생성하고 DoD(총 77·C6_1 0건·C54/C56 EXCLUDE·KEEP\* 재현·분위수 vs σ)를 검증한다.

**Files:**
- Regenerate: `src/agent_b_spc/limits/*`
- Modify: `src/agent_b_spc/analysis/false_alarm_mechanism.py`

- [ ] **Step 1: 재생성**

Run:
```bash
python -m src.agent_b_spc.initial_limits
python -m src.agent_b_spc.monitoring_whitelist
```
Expected: 로그에 "관리선 그룹: 77건" 부근, 판정 분포 출력.

- [ ] **Step 2: DoD 검증 스크립트**

Run:
```bash
python - <<'PY'
import pandas as pd
lim = pd.read_csv("src/agent_b_spc/limits/initial_limits_v1.csv")
wl = pd.read_csv("src/agent_b_spc/limits/monitoring_whitelist_v1.csv")

# 1) 총 그룹 수 = 77
assert len(lim) == 77, f"총 그룹 {len(lim)} != 77"
# 1b) 선검증 게이트 ① — 전 그룹 n≥100 (설계 §6 DoD 5). ②(t0 연속성)은 R1 필터로 구조 보장(미구현).
assert (lim["n_wafers"] >= 100).all(), f"n<100 그룹: {lim[lim.n_wafers<100][['sensor_id','step']].to_dict('records')}"
# 2) C6_1 0건 (전 decision 포함)
assert (wl["recipe_id"] == "C6_1").sum() == 0, "C6_1 잔존"
assert (lim["recipe_id"] == "C6_1").sum() == 0
# 3) C54/C56 정착 (실측 R6차): step1/5/6/7 = zero_sigma EXCLUDE / step4 = KEEP(광폭)
c5456 = wl[(wl.sensor_id.isin(["C54","C56"])) & (wl.sensor_window=="settled")]
excl = c5456[c5456.step.isin([1,5,6,7])]
s4 = c5456[c5456.step == 4]
assert (excl["decision"] == "EXCLUDE").all(), f"상수0 step이 EXCLUDE 아님: {excl[['sensor_id','step','decision']].to_dict('records')}"
assert (s4["decision"] == "KEEP").all(), f"step4가 KEEP 아님(스펙: sigma KEEP·KEEP* 무추가): {s4[['sensor_id','step','decision']].to_dict('records')}"
# step4는 밴드 광폭이라 사실상 무력 — W7 백로그(스펙 §3-2). 여기선 KEEP 기록만.
# 4) 기존 KEEP* 6그룹 재현 확인 (이탈 시 기록)
known = {("C61",1),("C61",6),("C61",7),("C57",4),("C58",4),("C31",5)}
keepstar = set(zip(wl[wl.decision=="KEEP*"].sensor_id, wl[wl.decision=="KEEP*"].step))
missing = known - keepstar
print("KEEP* 재현:", sorted(keepstar), "| 이탈(2% 미만 하락 등):", sorted(missing))
# 5) method 정합
assert (wl[wl.decision=="KEEP*"]["method"] == "quantile").all()
assert (wl[wl.decision=="KEEP"]["method"] == "sigma").all()
# 6) KEEP* σ-FA 요약 (CSV 기준). 분위수 vs σ 동일표본 완전비교는 Step 3의
#    false_alarm_mechanism.py가 6그룹에서 σ-FA·분위수-FA를 함께 산출(설계 §6-1).
ks = lim.merge(wl[["sensor_id","step","sensor_window","decision"]],
               on=["sensor_id","step","sensor_window"])
ks = ks[ks.decision=="KEEP*"]
print("KEEP* σ-FA 평균:", round(ks["false_alarm_pct"].mean(),2), "% (분위수 FA 완전비교는 false_alarm_mechanism.py)")
print("DoD OK — 총 77·n≥100·C6_1 0·C54/C56(step4 KEEP·나머지 EXCLUDE)·method 정합")
PY
```
Expected: `DoD OK ...`. 실패 시 해당 assert 지점 조사(총 그룹 수 어긋나면 §6-3 정산식 재점검).

- [ ] **Step 3: `false_alarm_mechanism.py` 복귀 블록 기준으로 갱신**

`analysis/false_alarm_mechanism.py`의 `post` 정의(현재 20행 `post = set(w.iloc[ri:]["C64"])`)를 복귀 블록으로 교체:

```python
seg = w.iloc[ri:].merge(df.groupby("C64")["C6"].first().rename("c6"), on="C64")
last_c61 = seg[seg.c6 == "C6_1"]["t0"].max()
post = set(seg[(seg.c6 == "C6_0") & (seg.t0 > last_c61)]["C64"])   # 복귀 블록(최근 레짐) — 스펙 R1 정합
```
헤더 docstring에 "표본 = 복귀 블록(설계 R1 정합, 2026-07-12)" 한 줄 추가.

이어서 **실행해 σ-FA·분위수-FA를 동일 표본에서 함께 산출**(설계 §6-1 완전 근거)하고, 6그룹 전부 분위수-FA ≤ σ-FA인지 눈으로 확인:

Run: `python -m src.agent_b_spc.analysis.false_alarm_mechanism`
Expected: 표에 `오탐%`(σ) 대비 `분위수후%`가 각 그룹에서 낮음(구성상 ~0.27% 부근). 인용 정합 확인.

- [ ] **Step 4: 재생성물 + 스크립트 커밋**

```bash
git add src/agent_b_spc/limits/ src/agent_b_spc/analysis/false_alarm_mechanism.py
git commit -m "feat: v1 baseline 재생성(4결정·2-track·77그룹) + false_alarm_mechanism 복귀블록 정합"
```

- [ ] **Step 5: meta `decisions_applied` + qa 메모 확인**

`initial_limits.py` `main()`의 meta 저장부에 `decisions_applied` 키가 있는지 확인, 없으면 추가:

```python
    meta |= {
        ...
        "decisions_applied": ["C6_1_baseline_excluded", "C54_C56_settled", "D_CH5960_excluded",
                              "combo2_quantile_2track", "config_migration"],
        "notes": ["C54/C56 정착 step4=KEEP(광폭 ±3σ, self-FA 0% — 사실상 무력, W7 백로그), "
                  "step1/5/6/7=상수0(zero_sigma→EXCLUDE). step5 부분보고 n≈2,397(값 0). "
                  "물리 확인 백로그: step4 광폭이 신호인지 두 위치 분산인지(W7 감시 유효성 재검토)."],
    }
```
재생성 후 `initial_limits_v1_meta.json`에 반영되면 커밋:
```bash
git add src/agent_b_spc/initial_limits.py src/agent_b_spc/limits/initial_limits_v1_meta.json
git commit -m "docs: meta에 decisions_applied·C54/C56 정착 실측 메모(step4 KEEP 광폭·step1/5/6/7 EXCLUDE) 기록"
```

---

### Task 9: 문서 갱신 (README·회의안건)

**Files:**
- Modify: `src/agent_b_spc/README.md`, `src/agent_b_spc/회의안건_요약_B.md`

- [ ] **Step 1: README 갱신**

`src/agent_b_spc/README.md`에서:
- 산출 방법 그룹 수 `148그룹` → `77그룹`, KEEP/KEEP\* 수를 Task 8 재생성 실측으로 갱신.
- 표본 정의 `8,144(전 C6_0)` → `6,824(복귀 블록)` 정합 (excluded_experimental 1,330과 일치, 산출 무영향 명시).
- 센서 구성: C54·C56 정착·D_CH5960 제외 반영, `SENSORS_TRANSIENT_ONLY`를 `[C18,C27]`로.
- 2-track: `limit_method` 표기를 DDL 정합 `method`로 통일.
- C54/C56 정착 실측 한 줄 추가: step1/5/6/7=EXCLUDE(상수0), step4=KEEP(광폭 ±3σ, 사실상 무력 — W7 백로그).

- [ ] **Step 2: 회의안건 요약 체크박스**

`src/agent_b_spc/회의안건_요약_B.md`의 로드맵 1단계 항목을 `[x]`로 갱신하고, 🔌 C(fdc.alert 소비자)에 "C54/C56 settled 통보" 후속을 명시(§7 문서 후속과 정합).

- [ ] **Step 3: 커밋**

```bash
git add src/agent_b_spc/README.md src/agent_b_spc/회의안건_요약_B.md
git commit -m "docs: baseline 재생성 반영 — README(77그룹·6,824·method·C54C56 실측) + 회의안건 1단계 체크"
```

- [ ] **Step 4: 최종 회귀**

Run: `python -m pytest src/agent_b_spc/tests/ -q`
Expected: 전체 PASS (기존 47 + 신규)

---

## Self-Review (완료)

- **Spec coverage**: §2 2-track(Task 6)·§3-1 C6_1 복귀블록(Task 4)·§3-2 C54/56 정착·**modality 게이트 폐기·W7 위임**(Task 5)·§3-3 D_CH5960(Task 5)·§4 config 이관(Task 1·2)+q등재(Task 3)·§5 죽은코드/게이트(Task 4)·§6 DoD(Task 8)·§7 문서(Task 9)·§9 커밋 2단(Stage A/B) 전부 대응.
- **Placeholder scan**: 각 코드 스텝에 실제 코드·명령·기대출력 포함. 없음.
- **Type consistency**: `LimitConfig`(Task 1, Task 3서 q_low/q_high 추가)·`WhitelistConfig`(Task 2)·`compute_limits(summaries, cfg)`·`classify(limits, cfg)`·`determine_sample_wafers(df, cfg)` 시그니처 태스크 간 일치. (Task 7 modality 함수 폐기 — 미도입.)
