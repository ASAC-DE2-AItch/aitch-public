# -*- coding: utf-8 -*-
"""YAML 시나리오 주입기 — P3-1.

시나리오 YAML을 읽어 지정 챔버·센서에 이상 패턴을 주입하고,
주입 이벤트를 sim_events(JSONL — 답안지)로 기록한다.

패턴 (v1.1):
  - drift       : duration_wafers에 걸쳐 0 → magnitude_sigma×std 선형 램프 후 유지
  - shift       : start 시점부터 magnitude_sigma×std 계단 이동
  - spike       : start 시점 wafer 1장만 magnitude_sigma×std (crazy wafer 재현)
  - oscillation : ±magnitude_sigma×std 교대 (N4 유도, period_wafers 주기 — 기본 매 wafer 교대)

σ 단위 원칙 (2026-07-31 개정 — PM): magnitude_sigma 의 "σ" = **그 행이 속한
(recipe, step, C42) 구간 std** (std_lookup 주입 시). 관리선·B 판정·S1 표시와 같은 자다.
구 전역 std 해석은 스텝 계단 센서에서 4σ 지정이 관리선 155σ 로 팽창했다 (현업 비현실 —
노이즈 스케일 수정(2026-07-30)과 동일 원리의 잔재). std_lookup 미주입(레거시)은 전역 유지.

패턴 (v1.2 — P5-3, 2026-07-20):
  - pm_reset    : 가상 PM(요란 레짐 전환) — 실측 리셋 사건(train reset, N=300 창)의 재현.
      · level_shifts_eng    : {센서: Δ공학단위} — 정착 레벨 영구 계단 (예: C17 −37.7)
      · ignition_shifts_eng : {센서: Δ공학단위} — 과도(stabilization_flag=1) 행에만 적용
                              (예: C11 +8.85 → wf_min이 얕아짐, F14 재현. 정착 평균은 침묵 유지)
      · y_shift             : C65(라벨) 영구 이동 — fdc.actual 경유만 (raw 미포함, 헌법 1-3)
      · C33 리베이스        : 트리거 시점부터 1로 재시작 (B의 PM 감지 입력 정합)
      · pm_count 가산       : producer가 pm_bumps()로 합산 → QualPublisher 자동 트리거
      단위 원칙: 이 패턴만 σ가 아니라 **공학단위** — σ 분모(row-std vs wafer-mean-std) 모호성 차단,
      실측 델타를 그대로 기입한다. C9는 실측상 이동 없음(레짐 k≈0) — 주입하지 않는 것이 재현.

대상 지정 (v1.1 — 하위 호환):
  - target_chamber(str) 또는 target_chambers(list) — multi_chamber 시나리오용
  - sensor(str) 또는 sensors(list) — alarm storm(다중 센서 동시)용 (pm_reset은 shifts 키에서 자동 유도)

경계 (화면 정의서 S11): 주입은 fdc.raw 위쪽(데이터 생성)만 제어.
파이프라인은 sim_events를 읽지 않는다 (헌법 — 답안지 커닝 금지).
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

log = logging.getLogger("scenario-injector")

REQUIRED_KEYS = {"scenario_id", "pattern", "start_after_wafers",
                 "magnitude_sigma", "ground_truth"}
PATTERNS = {"drift", "shift", "spike", "oscillation", "pm_reset"}
DEFAULT_OSC_PERIOD = 2  # oscillation 기본 주기(wafer) — 매 wafer 교대 (N4 유도)


def normalize_scenario(sc: dict, origin: str = "<dict>") -> dict:
    """시나리오 dict 필수 키 검증 + 대상 필드 정규화 (실패 시 명시적 에러).

    load_scenario(파일)와 InjectControl(파일드롭 — M3 ②)이 공유하는 단일 검증 소스.
    반환 dict에는 정규화된 `chambers`(list)·`sensors`(list)가 추가된다.
    """
    # pm_reset은 공학단위 shifts 기반 — magnitude_sigma 면제 (문서 헤더 단위 원칙)
    required = REQUIRED_KEYS - ({"magnitude_sigma"} if sc.get("pattern") == "pm_reset" else set())
    missing = required - set(sc)
    if missing:
        raise ValueError(f"시나리오 {origin} 필수 키 누락: {missing}")
    if sc["pattern"] not in PATTERNS:
        raise ValueError(f"지원하지 않는 pattern: {sc['pattern']} (지원: {PATTERNS})")
    if sc["pattern"] == "drift" and "duration_wafers" not in sc:
        raise ValueError("drift 패턴은 duration_wafers 필수")
    if sc["pattern"] == "pm_reset":
        sc.setdefault("level_shifts_eng", {})
        sc.setdefault("ignition_shifts_eng", {})
        sc.setdefault("y_shift", 0.0)
        if not sc["level_shifts_eng"] and not sc["ignition_shifts_eng"] and not sc["y_shift"]:
            raise ValueError("pm_reset은 level_shifts_eng / ignition_shifts_eng / y_shift 중 1개 이상 필수")

    # 대상 정규화 — 단수/복수 키 하위 호환
    chambers = sc.get("target_chambers") or ([sc["target_chamber"]] if "target_chamber" in sc else None)
    if sc["pattern"] == "pm_reset":
        # pm_reset의 sensors = shifts 키 합집합 (yaml에 sensor 키 불요)
        sensors = sorted(set(sc["level_shifts_eng"]) | set(sc["ignition_shifts_eng"]))
    else:
        sensors = sc.get("sensors") or ([sc["sensor"]] if "sensor" in sc else None)
    if not chambers:
        raise ValueError(f"시나리오 {origin}: target_chamber(s) 필수")
    if not sensors:
        raise ValueError(f"시나리오 {origin}: sensor(s) 필수")
    sc["chambers"] = list(chambers)
    sc["sensors"] = list(sensors)
    return sc


def load_scenario(path: Path) -> dict:
    """시나리오 YAML 로드 → normalize_scenario (utf-8-sig — BOM 내성, 헌법 §7 PS1 교훈)."""
    with open(path, encoding="utf-8-sig") as f:
        sc = yaml.safe_load(f)
    if not isinstance(sc, dict):
        raise ValueError(f"시나리오 {path}: YAML 최상위가 dict가 아님")
    return normalize_scenario(sc, origin=str(path))


class ScenarioInjector:
    """주입 상태 머신 — 챔버별 wafer 카운트 기준으로 패턴 적용."""

    def __init__(self, scenarios: list[dict], sensor_std: dict[str, float],
                 *, std_lookup=None,
                 events_path: Path = Path("sim_events.jsonl")):
        self.scenarios = scenarios
        self.sensor_std = sensor_std
        self._std_lookup = std_lookup            # (col, recipe, step, c42) → std | None (σ 단위 원칙)
        self.events_path = events_path
        self._announced: set[tuple[str, str]] = set()  # (scenario_id, chamber)
        self._c33_base: dict[tuple[str, str], float] = {}  # pm_reset C33 리베이스 기준

    def _record_event(self, sc: dict, chamber_id: str, chamber_wafer_index: int):
        """주입 시작 시 답안지(JSONL) 기록 — DB 적재는 P4에서 sim_events 테이블로 전환."""
        event = {
            "event_id": f"SIMEV-{sc['scenario_id']}-{chamber_id}",
            "scenario_id": sc["scenario_id"],
            "chamber_id": chamber_id,
            "sensor_ids": sc["sensors"],
            "pattern": sc["pattern"],
            "magnitude_sigma": sc.get("magnitude_sigma"),
            "injected_at_wafer_index": chamber_wafer_index,
            "injected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ground_truth": sc["ground_truth"],
        }
        if sc["pattern"] == "pm_reset":
            event["level_shifts_eng"] = sc["level_shifts_eng"]
            event["ignition_shifts_eng"] = sc["ignition_shifts_eng"]
            event["y_shift"] = sc["y_shift"]
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        log.info(f"💉 주입 시작: {sc['scenario_id']} ({sc['pattern']} {sc['sensors']} "
                 f"@{chamber_id}, wafer#{chamber_wafer_index})")

    def _announce(self, sc: dict, chamber_id: str, chamber_wafer_index: int):
        """최초 활성 시 1회 답안지 기록 — delta/pm_bumps 어느 쪽이 먼저 와도 1회."""
        key = (sc["scenario_id"], chamber_id)
        if key not in self._announced:
            self._announced.add(key)
            self._record_event(sc, chamber_id, chamber_wafer_index)

    def _active(self, chamber_id: str, chamber_wafer_index: int, pattern: str | None = None):
        """이 (챔버, 시점)에 활성(k≥0)인 시나리오 제너레이터 — (sc, k) yield."""
        for sc in self.scenarios:
            if pattern is not None and sc["pattern"] != pattern:
                continue
            if chamber_id not in sc["chambers"]:
                continue
            k = chamber_wafer_index - sc["start_after_wafers"]
            if k < 0:
                continue
            yield sc, k

    @staticmethod
    def pattern_delta(sc: dict, std: float, k: int) -> float:
        """주입 시작 후 k(≥0)번째 wafer에서 단일 시나리오의 기여량 — 순수 함수.

        발행(delta)과 검증(dry_run 기대값)이 같은 산식을 쓰는 단일 소스.
        """
        if k < 0:
            return 0.0
        mag = sc["magnitude_sigma"] * std
        if sc["pattern"] == "shift":
            return mag
        if sc["pattern"] == "spike":
            return mag if k == 0 else 0.0
        if sc["pattern"] == "drift":
            dur = max(1, sc["duration_wafers"])
            return mag * min(1.0, k / dur)
        if sc["pattern"] == "oscillation":
            period = max(2, int(sc.get("period_wafers", DEFAULT_OSC_PERIOD)))
            return mag if (k % period) < period / 2 else -mag
        return 0.0

    def delta(self, chamber_id: str, sensor: str, chamber_wafer_index: int,
              transient: bool = False, recipe=None, step=None, c42=None) -> float:
        """이 (챔버, 센서, wafer 시점)에 더할 주입량 (전 시나리오 스택 합). 없으면 0.

        transient=True — 이 행이 과도 구간(stabilization_flag=1)임을 뜻한다.
        pm_reset의 ignition_shifts_eng는 과도 행에만 얹힌다 (wf_min 재현, 정착 평균 침묵 — F14).
        recipe/step/c42 — σ 단위 원칙(모듈 도스트링): std_lookup 이 있으면 그 행의 구간 std 로
        해석하고, 구간 std 미정의(상수 구간 등)면 **주입 생략** (perturb 노이즈와 동일 폴백 —
        전역 std 로 떨어지지 않는다). 컨텍스트 미전달(레거시 호출)은 전역 std 유지.
        """
        total = 0.0
        for sc, k in self._active(chamber_id, chamber_wafer_index):
            if sensor not in sc["sensors"]:
                continue
            self._announce(sc, chamber_id, chamber_wafer_index)
            if sc["pattern"] == "pm_reset":
                total += float(sc["level_shifts_eng"].get(sensor, 0.0))
                if transient:
                    total += float(sc["ignition_shifts_eng"].get(sensor, 0.0))
            else:
                if self._std_lookup is not None and (recipe is not None or step is not None):
                    std = self._std_lookup(sensor, recipe=recipe, step=step, c42=c42) or 0.0
                else:
                    std = self.sensor_std.get(sensor, 0.0)
                total += self.pattern_delta(sc, std, k)
        return total

    # ---- pm_reset 전용 훅 (P5-3) --------------------------------------------

    def pm_bumps(self, chamber_id: str, chamber_wafer_index: int) -> int:
        """가상 PM 가산분 — producer가 replicator pm_count에 더한다 (Qual 자동 트리거)."""
        n = 0
        for sc, _k in self._active(chamber_id, chamber_wafer_index, pattern="pm_reset"):
            self._announce(sc, chamber_id, chamber_wafer_index)
            n += 1
        return n

    def c33_rebase(self, chamber_id: str, chamber_wafer_index: int, c33: float) -> float:
        """가상 PM 이후 C33을 1부터 재시작 — B의 PM 감지 입력(C33 급락) 정합.

        최초 관측된 원본 C33을 기준으로 (원본 − 기준 + 1). 원본 증가 리듬은 보존된다.
        """
        for sc, _k in self._active(chamber_id, chamber_wafer_index, pattern="pm_reset"):
            key = (sc["scenario_id"], chamber_id)
            if key not in self._c33_base:
                self._c33_base[key] = c33
            c33 = c33 - self._c33_base[key] + 1.0
        return c33

    def y_delta(self, chamber_id: str, chamber_wafer_index: int) -> float:
        """C65(라벨) 이동 합 — fdc.actual 발행 직전에 가산 (raw 미포함, 헌법 1-3)."""
        return sum(float(sc["y_shift"])
                   for sc, _k in self._active(chamber_id, chamber_wafer_index, pattern="pm_reset"))

    # ---- 월드스테이트 개조 (M3 ② — 실행 중 주입 + 잔류 승계) ---------------------

    def inject_now(self, sc: dict, at_index: int) -> dict:
        """실행 중 주입 — start_after_wafers를 현재 챔버 순번(at_index)으로 스탬프해 장전.

        상시프로세스가 도는 중 S11 클릭에 대응하는 런타임 주입. 이후 이 시나리오의 효과는
        at_index부터 k=0으로 시작하고, **shift·drift(램프 후 유지)·pm_reset은 원복되지
        않으므로** 컷이 바뀌어도 잔류한다 (§0-2 잔류 승계 맵 — 원복 금지가 존재 이유).

        원본 dict를 복제해 장전한다 (YAML 원본 불변 — 같은 시나리오를 다른 컷/챔버에
        재주입해도 서로 간섭하지 않음). 반환값은 장전된 사본.
        """
        loaded = dict(sc)
        loaded["start_after_wafers"] = int(at_index)
        self.scenarios.append(loaded)
        return loaded

    def active_summary(self, idx_by_chamber: dict[str, int]) -> dict[str, list[str]]:
        """현재 월드스테이트 — 챔버별 활성(k≥0) 시나리오 id 목록 (원복 금지 가시화).

        idx_by_chamber: {chamber_id: 현재 ch_wafer_index}. 컷이 진행될수록 활성 집합은
        (잔류 패턴에 한해) 단조 증가한다 — 체인 dry-run의 잔류 승계 판정 소스.
        """
        out: dict[str, list[str]] = {}
        for ch, idx in idx_by_chamber.items():
            act = [sc["scenario_id"] for sc in self.scenarios
                   if ch in sc["chambers"] and (idx - sc["start_after_wafers"]) >= 0]
            if act:
                out[ch] = act
        return out
