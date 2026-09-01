# -*- coding: utf-8 -*-
"""P4-4 — mock fdc.alert publisher (시뮬레이터 소유 임시 발행기).

목적: B4-1(Context Score + fdc.alert 공동 구현, ~7/21) 완료 전에
C(통합 Agent Service, C4-1)와 PM(Incident 그룹핑 백엔드, P4-3)이
실제 `fdc.alert` 토픽으로 개발을 시작할 수 있게 하는 계약 준수 가짜 경보 발행기.

원칙:
  - 계약 포크 아님 — 페이로드는 API Contract §3(fdc.alert) 단일 소스 그대로.
  - 실제 토픽(fdc.alert) 사용 — 구독 경로가 본구현과 동일 (헌법 1-2 정합).
  - 탐지 로직 없음 — B의 판정을 흉내내는 것이 아니라 B의 "출력 형태"만 재현.
  - B4-1 완료 시 영구 오프 (실행 중지). 코드 정리는 W5 시점에.

모드:
  drift : N3 추세 위반 WARNING (SIM_CH_3/C11 — SC0·SC1 시나리오와 동일 상황)
  spike : N1 급변 CRITICAL (SIM_CH_1/C31, predicted_c65 > B9 임계 — crazy wafer 상황)
  storm : 같은 챔버(SIM_CH_2) 다중 센서 연속 6건 — P4-3 Incident 그룹핑(M13) 테스트
  multi : B3(≥4)개 챔버 동일 센서(C17) + reference_suspect=true — 역방향 룰 상황
  quiet : context_score < B7(31) — Agent 미가동·기록만 경로 테스트
  mix   : 단건류 가중 순환 (기본 — drift 2 : quiet 1 : spike 1)

사용법:
    python -m src.simulator.mock_alert_publisher --dry-run --count 40 --out mock_alerts_sample.jsonl
    python -m src.simulator.mock_alert_publisher --mode storm --count 12 --interval 1
    python -m src.simulator.mock_alert_publisher                    # mix, 2초 간격, count 무한
"""

import argparse
import json
import logging
import random
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .message_builder import ROOT, load_params

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mock-alert")

TOPIC_ALERT = "fdc.alert"
SENSOR_MAP_PATH = ROOT / "config" / "sensor_map.yaml"  # 있으면 표시명 병기 (없어도 동작)
ALERT_ID_RE = re.compile(r"^ALERT-\d{8}-[A-Z0-9]+-\d{4,}$")  # 6-4 업무 ID 포맷 — SEQ 는 4자리 "최소폭"
# (A33 · 2026-08-08 실측) 실발행기 next_alert_id 는 DB max+1 채번이라 seq 가 9999 를 넘으면
# 5자리로 발급된다 — 8/06 SIMCH1 11,311 실물 확인. `\d{4}`(정확히 4)로 두면 실데이터와 계약이
# 어긋난다. `:04d` 는 zero-pad 최소폭일 뿐 상한이 아니다.

# 모드별 페이로드 템플릿 재료 (mock 상수 — 물리값은 계약 예시 수준의 그럴듯한 값)
SENSOR_TEMPLATES = {
    "C11": {"current": -302.0, "ucl": -280.0, "lcl": -340.0},
    "C17": {"current": 168.0, "ucl": 245.0, "lcl": 152.0},
    "C62": {"current": 4180.0, "ucl": 4150.0, "lcl": 3620.0},
    "C31": {"current": 152.0, "ucl": 149.0, "lcl": 121.0},
}
RULE_DESC = {
    "N1": "1 point beyond 3 sigma",
    "N3": "6 points continuously increasing",
    "N5": "2 of 3 points beyond 2 sigma (same side)",
}
STORM_CHAMBER = "SIM_CH_2"
STORM_SENSOR_RULES = [("C11", "N5"), ("C17", "N3"), ("C62", "N1"),
                      ("C11", "N3"), ("C17", "N5"), ("C62", "N5")]
MULTI_SENSOR = "C17"
MIX_ROTATION = ["drift", "quiet", "drift", "spike"]  # 가중 순환 (2:1:1)

# context_score 대역 (mock 전용 — 판정 산식 아님, B4-1이 본구현)
SCORE_BAND = {"drift": (55, 75), "spike": (85, 97), "storm": (70, 90),
              "multi": (60, 80)}
PREDICTED_BAND = {"drift": (700, 900), "spike": (1590, 1700), "storm": (900, 1200),
                  "multi": (800, 1000), "quiet": (640, 720)}


def _load_sensor_map() -> dict:
    """config/sensor_map.yaml 있으면 로드 (표시 계층 매핑 — 없으면 빈 dict)."""
    if SENSOR_MAP_PATH.exists():
        with open(SENSOR_MAP_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


class MockAlertFactory:
    """계약 §3 스키마의 mock alert 생성기 — 시드 고정으로 재현 가능."""

    def __init__(self, params: dict, seed: int = 42):
        self.rng = random.Random(seed)
        self.agent_min = params["spc"]["context_score_agent_min"]      # B7
        self.reverse_min = params["spc"]["reverse_rule_min_chambers"]  # B3
        self.crazy_threshold = params["spc"]["crazy_wafer"]["predicted_c65_threshold"]  # B9
        self.sensor_map = _load_sensor_map()
        # alert_id 시퀀스 — 재시작 간 충돌 방지를 위해 분 단위 시각으로 시드
        # (A33) 구현이 `% 10000` 으로 감았었다 — 한 실행에서 1만 건을 넘기면 id 재사용
        # (하류 그루퍼가 신규 알람을 "중복 스킵"으로 오인 = 조용한 유실). 실측상 챔버당
        # 하루 11,311건이 실제로 나오므로 감기 제거. 실발행기(DB max+1)와 동일하게 단조 증가.
        self._seq = (int(time.time()) // 60) % 10000

    def _next_id(self, chamber_id: str) -> str:
        """업무 ID 포맷 (6-4): ALERT-<YYYYMMDD>-<CHAMBER>-<SEQ ≥ 4자리>."""
        self._seq += 1
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        return f"ALERT-{date}-{chamber_id.replace('_', '')}-{self._seq:04d}"

    def _violation(self, sensor: str, rule: str, severity: str, window: str) -> dict:
        """violations[] 항목 1건."""
        t = SENSOR_TEMPLATES[sensor]
        v = {
            "rule_id": rule,
            "sensor": sensor,
            "window": window,
            "severity": severity,
            "description": RULE_DESC[rule],
            "current_value": round(t["current"] + self.rng.uniform(-1.0, 1.0), 2),
            "limit_version": "v1",
            "control_limit_upper": t["ucl"],
            "control_limit_lower": t["lcl"],
        }
        name = self.sensor_map.get(sensor)
        if name:  # 표시명은 매핑 파일 있을 때만 병기 (코드 하드코딩 금지 — 헌법 6-4)
            v["sensor_name"] = name
        return v

    def _base(self, chamber_id: str, kind: str, violations: list[dict],
              score: int, tttm_score: float, reference_suspect: bool = False) -> dict:
        """공통 페이로드 골격 (계약 §3)."""
        wafer_no = self.rng.randint(1, 11939)
        suffix = chamber_id.replace("SIM_", "")
        wafer_id = f"C64_{wafer_no}_{suffix}"
        top_sensor = violations[0]["sensor"]
        lo, hi = PREDICTED_BAND[kind]
        return {
            "alert_id": self._next_id(chamber_id),
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "chamber_id": chamber_id,
            "violations": violations,
            "tttm": {
                "reference": "fleet_median",
                "score": round(tttm_score, 2),
                "top_gap_sensor": top_sensor,
                "gap_pct": round(self.rng.uniform(1.5, 6.0), 1),
                "reference_suspect": reference_suspect,
            },
            "context_score": score,
            "suspect_window": {
                "start_wafer": f"C64_{max(1, wafer_no - 20)}_{suffix}",
                "end_wafer": wafer_id,
                "basis": f"{violations[0]['rule_id']} 추세 시작점 소급",
            },
            "prediction_context": {
                "wafer_id": wafer_id,
                "predicted_c65": round(self.rng.uniform(lo, hi), 1),
                "shap_top3": [top_sensor] + [s for s in ("C62", "C17", "C11")
                                             if s != top_sensor][:2],
                "spc_flags": [{"sensor": v["sensor"], "rule": v["rule_id"]}
                              for v in violations],
            },
        }

    def _score(self, kind: str) -> int:
        """모드 대역 내 context_score (mock)."""
        lo, hi = SCORE_BAND[kind]
        return self.rng.randint(lo, hi)

    def make(self, kind: str) -> list[dict]:
        """모드 1회분 alert 리스트 생성 (storm·multi는 버스트)."""
        if kind == "drift":
            v = [self._violation("C11", "N3", "WARNING", "settled")]
            return [self._base("SIM_CH_3", "drift", v, self._score("drift"), 2.3)]
        if kind == "spike":
            v = [self._violation("C31", "N1", "CRITICAL", "transient")]
            return [self._base("SIM_CH_1", "spike", v, self._score("spike"), 3.4)]
        if kind == "quiet":
            v = [self._violation("C62", "N5", "WARNING", "settled")]
            return [self._base("SIM_CH_4", "quiet", v,
                               self.rng.randint(10, self.agent_min - 1), 1.2)]
        if kind == "storm":
            burst = []
            for sensor, rule in STORM_SENSOR_RULES:
                sev = "CRITICAL" if rule == "N1" else "WARNING"
                v = [self._violation(sensor, rule, sev, "settled")]
                burst.append(self._base(STORM_CHAMBER, "storm", v,
                                        self._score("storm"), 3.1))
            return burst
        if kind == "multi":
            # 4챔버 세계관 (v1.3) — 역방향 룰 ≥3/4: CH3(주인공) 제외 3챔버 동시 이탈
            chambers = [f"SIM_CH_{i}" for i in (1, 2, 4)][: max(self.reverse_min, 3)]
            burst = []
            for ch in chambers:
                v = [self._violation(MULTI_SENSOR, "N5", "WARNING", "settled")]
                burst.append(self._base(ch, "multi", v, self._score("multi"),
                                        3.2, reference_suspect=True))
            return burst
        raise ValueError(f"지원하지 않는 mode: {kind}")

    def generate(self, mode: str, count: int) -> list[tuple[str, dict]]:
        """mode에 따라 count건 생성 — (kind, alert) 쌍 반환 (검증기가 종류별 판정에 사용).

        mix는 가중 순환, 버스트류(storm·multi)는 회분 단위로 채움.
        """
        out: list[tuple[str, dict]] = []
        i = 0
        while len(out) < count:
            kind = MIX_ROTATION[i % len(MIX_ROTATION)] if mode == "mix" else mode
            out.extend((kind, m) for m in self.make(kind))
            i += 1
        return out[:count]


# ---------------------------------------------------------------------------
# 검증 (dry-run) — 계약 §3 준수 자동 채점
# ---------------------------------------------------------------------------

REQUIRED_TOP = {"alert_id", "timestamp", "chamber_id", "violations", "tttm",
                "context_score", "suspect_window", "prediction_context"}
REQUIRED_VIOLATION = {"rule_id", "sensor", "window", "severity", "description",
                      "current_value", "limit_version",
                      "control_limit_upper", "control_limit_lower"}


def validate_alert(msg: dict) -> list[str]:
    """alert 1건의 계약 위반 목록 (없으면 빈 리스트)."""
    p = []
    missing = REQUIRED_TOP - set(msg)
    if missing:
        p.append(f"필수 필드 누락: {sorted(missing)}")
        return p
    if not ALERT_ID_RE.match(msg["alert_id"]):
        p.append(f"alert_id 포맷 위반: {msg['alert_id']}")
    if not msg["chamber_id"].startswith("SIM_CH_"):
        p.append(f"chamber_id 규칙 위반: {msg['chamber_id']}")
    if not msg["violations"]:
        p.append("violations 비어 있음")
    for v in msg["violations"]:
        miss = REQUIRED_VIOLATION - set(v)
        if miss:
            p.append(f"violation 필드 누락: {sorted(miss)}")
        if v.get("window") not in ("transient", "settled"):
            p.append(f"window 값 위반: {v.get('window')}")
        if v.get("severity") not in ("WARNING", "CRITICAL"):
            p.append(f"severity 값 위반: {v.get('severity')}")
    if not isinstance(msg["context_score"], int) or not 0 <= msg["context_score"] <= 100:
        p.append(f"context_score 위반: {msg['context_score']}")
    if "wafer_id" not in msg["prediction_context"]:
        p.append("prediction_context.wafer_id 누락 (변경 금지 필드)")
    try:
        json.dumps(msg, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        p.append(f"JSON 직렬화 실패: {e}")
    return p


def run_dry(args, factory: MockAlertFactory) -> int:
    """count건 생성 → 계약·모드 거동 검증 리포트. FAIL 수 반환."""
    pairs = factory.generate(args.mode, args.count)
    alerts = [m for _, m in pairs]
    results = []

    # V1 계약 준수 (전 건)
    bad = [(a["alert_id"], probs) for a in alerts if (probs := validate_alert(a))]
    results.append(("V1 계약 §3 준수 (전 건)", not bad,
                    f"{len(alerts)}건 중 위반 {len(bad)}건"
                    + (f" — 첫 위반 {bad[0]}" if bad else "")))

    # V2 alert_id 유일성
    ids = [a["alert_id"] for a in alerts]
    results.append(("V2 alert_id 유일성", len(ids) == len(set(ids)),
                    f"{len(set(ids))}/{len(ids)} 고유"))

    # V3 모드 거동
    if args.mode == "storm":
        same = all(a["chamber_id"] == STORM_CHAMBER for a in alerts)
        sensors = {v["sensor"] for a in alerts for v in a["violations"]}
        ok = same and len(alerts) >= 5 and len(sensors) >= 2
        results.append(("V3 storm 거동 (단일 챔버·다중 센서 버스트 — M13 재료)", ok,
                        f"챔버 동일={same}, {len(alerts)}건, 센서 {len(sensors)}종"))
    elif args.mode == "multi":
        chambers = {a["chamber_id"] for a in alerts}
        suspect = all(a["tttm"]["reference_suspect"] for a in alerts)
        ok = len(chambers) >= factory.reverse_min and suspect
        results.append((f"V3 multi 거동 (챔버 ≥ B3={factory.reverse_min}·참조 의심)", ok,
                        f"챔버 {len(chambers)}개, reference_suspect 전건={suspect}"))
    elif args.mode == "mix":
        actionable = sum(1 for a in alerts if a["context_score"] >= factory.agent_min)
        quiet = sum(1 for a in alerts if a["context_score"] < factory.agent_min)
        ok = actionable >= 1 and quiet >= 1
        results.append((f"V3 mix 거동 (B7={factory.agent_min} 기준 가동/기록만 혼재)", ok,
                        f"가동 {actionable}건 / 기록만 {quiet}건"))
    else:
        results.append((f"V3 {args.mode} 거동", True, f"{len(alerts)}건 생성"))

    # V4 B9 정합 — spike '종류'로 생성된 alert만 crazy 임계 초과 대상
    #   (N1 룰 ≠ crazy wafer: storm의 N1 멤버는 의도적으로 비-crazy 대역 —
    #    M13 그룹핑 테스트가 B9 HOLD 경로와 섞이지 않게 격리)
    spikes = [m for k, m in pairs if k == "spike"]
    if spikes:
        ok = all(m["prediction_context"]["predicted_c65"] > factory.crazy_threshold
                 for m in spikes)
        results.append((f"V4 spike종 predicted_c65 > B9({factory.crazy_threshold})", ok,
                        f"spike종 {len(spikes)}건 전건 초과={ok}"))
    non_crazy = [m for k, m in pairs if k in ("storm", "multi", "drift", "quiet")]
    if non_crazy:
        ok = all(m["prediction_context"]["predicted_c65"] <= factory.crazy_threshold
                 for m in non_crazy)
        results.append((f"V4b 비-spike종 predicted_c65 ≤ B9 (경로 격리)", ok,
                        f"{len(non_crazy)}건 전건 이하={ok}"))

    if args.out:
        out = Path(args.out)
        with open(out, "w", encoding="utf-8") as f:
            for a in alerts:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")
        log.info(f"📦 샘플 {len(alerts)}건 저장: {out} (C·P4-3 오프라인 개발용)")

    fails = sum(1 for _, ok, _ in results if not ok)
    print("\n" + "=" * 78)
    print(f"P4-4 MOCK ALERT DRY-RUN 리포트 (mode={args.mode}, {len(alerts)}건)")
    print("=" * 78)
    for name, ok, detail in results:
        print(f"  {'PASS ✅' if ok else 'FAIL ❌':8} │ {name}\n           │   {detail}")
    print("-" * 78)
    print(f"  결과: {len(results) - fails}/{len(results)} PASS"
          + ("  →  P4-4 DoD 충족 🎉" if fails == 0 else "  →  FAIL — 로그 확인"))
    print("=" * 78)
    return fails


# ---------------------------------------------------------------------------
# 발행 (Kafka)
# ---------------------------------------------------------------------------

def run_publish(args, factory: MockAlertFactory) -> None:
    """fdc.alert 토픽으로 발행 루프 — graceful shutdown (헌법 6-2)."""
    from confluent_kafka import Producer  # 지연 import — dry-run은 Kafka 불요

    producer = Producer({"bootstrap.servers": args.bootstrap,
                         "client.id": "mock-alert-publisher"})
    stop = {"flag": False}

    def _shutdown(signum, _frame):
        log.info(f"⏹  신호 수신({signum}) — 정리 후 종료")
        stop["flag"] = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(f"🔌 Kafka {args.bootstrap} → {TOPIC_ALERT} │ mode={args.mode} │ "
             f"interval={args.interval}s │ count={args.count or '∞'} │ "
             f"⚠ B4-1 완료 시 이 발행기는 오프")
    sent = 0
    while not stop["flag"] and (args.count == 0 or sent < args.count):
        kind = (MIX_ROTATION[sent % len(MIX_ROTATION)]
                if args.mode == "mix" else args.mode)
        for msg in factory.make(kind):
            probs = validate_alert(msg)
            if probs:  # 발행 전 자기 검증 — 위반건은 스킵+로그 (죽지 않음)
                log.error(f"계약 위반으로 스킵: {probs}")
                continue
            producer.produce(topic=TOPIC_ALERT,
                             key=msg["chamber_id"].encode("utf-8"),
                             value=json.dumps(msg, ensure_ascii=False).encode("utf-8"))
            producer.poll(0)
            sent += 1
            log.info(f"📤 {msg['alert_id']} ({kind}, score={msg['context_score']}, "
                     f"@{msg['chamber_id']}) — {sent}건")
            if stop["flag"] or (args.count and sent >= args.count):
                break
            if kind in ("storm", "multi"):
                time.sleep(min(0.3, args.interval))  # 버스트 내 짧은 간격
        time.sleep(args.interval)
    producer.flush()
    log.info(f"✅ 종료 — {sent}건 발행")


def main():
    """CLI 진입점."""
    p = argparse.ArgumentParser(description="P4-4 mock fdc.alert publisher")
    p.add_argument("--mode", default="mix",
                   choices=["mix", "drift", "spike", "storm", "multi", "quiet"])
    p.add_argument("--count", type=int, default=0,
                   help="발행/생성 건수 (발행 모드 0=무한, dry-run 기본 40)")
    p.add_argument("--interval", type=float, default=2.0, help="발행 간격(초)")
    p.add_argument("--bootstrap", default="localhost:9092")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--dry-run", action="store_true", help="Kafka 없이 생성+검증만")
    p.add_argument("--out", default=None, help="샘플 JSONL 저장 경로 (dry-run)")
    args = p.parse_args()

    factory = MockAlertFactory(load_params(), seed=args.seed)
    if args.dry_run:
        if args.count == 0:
            args.count = 40
        sys.exit(1 if run_dry(args, factory) else 0)
    run_publish(args, factory)


if __name__ == "__main__":
    main()
