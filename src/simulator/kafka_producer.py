# -*- coding: utf-8 -*-
"""능동형 제조 AI 플랫폼 v4.5 — FDC 시뮬레이터 (P3-1: 멀티챔버 + 시나리오 주입).

train_data.csv를 Row(3초 샘플) 단위로 리플레이하여 가상 챔버 N대의
`fdc.raw` 스트림을 발행한다.

[P3-1 개정 — 2026-07-07, API Contract v4.4/4.5 정합화]
  - chamber_id = 시뮬레이터가 부여하는 가상 챔버 SIM_CH_1~4 (장비 1대=4PM, 시뮬 스펙 v1.3)
    (구버전의 chamber_id=C6 은 오류 — C6는 Recipe ID)
  - pm_count = 챔버별 C33(RF time) 리셋 감지 카운터 (C33≠pm_count — v4.5 멘토 확정)
  - 계약 필수 필드 추가: lot_id, slot_no, seq_in_step, elapsed_in_step,
    stabilization_flag, is_qual
  - 발표용 센서명 하드코딩(SENSOR_NAME_MAP) 삭제 — 헌법 6-4 (C코드 원칙)
  - 설정은 config/params.yaml 참조 (헌법 6-1)
  - YAML 시나리오 주입 (--scenario) + sim_events.jsonl 답안지 기록
  - graceful shutdown (SIGINT/SIGTERM — 헌법 6-2)

사용법:
    python -m src.simulator.kafka_producer                            # 4챔버(params E1), 0.1초 간격
    python -m src.simulator.kafka_producer --scenario src/simulator/scenarios/drift_c11.yaml
    python -m src.simulator.kafka_producer --delay 0 --limit 500      # 최대 속도, wafer 500장
    python -m src.simulator.kafka_producer --loop                     # 무한 리플레이
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import pandas as pd
from confluent_kafka import Producer

from .chamber_replicator import ChamberReplicator
from .message_builder import (ROOT, DEFAULT_CSV_PATH, build_message,
                              filter_post_reset, load_params)
from .scenario_injector import ScenarioInjector, load_scenario
from .inject_control import InjectControl
from .actual_publisher import ActualPublisher
from .qual_publisher import QualPublisher
from .recipe_applier import (CONSUMER_GROUP, TOPIC_CORRECTION, RecipeApplier,
                             poll_corrections)
from . import single_instance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fdc-simulator")

# 경로·메시지 조립·스키마 상수는 message_builder로 이동 (P3-1 — dry_run과 공용)
# 브로커 주소 — **env 우선, 호스트 기본값 폴백** (2026-08-07, #126 C 리뷰 🔴).
#   컨테이너에서는 `localhost:9092` 가 자기 자신을 가리켜 Kafka 에 못 붙는다. 그런데
#   confluent_kafka producer 는 **연결 실패를 예외로 안 내고 로컬 큐에 쌓는다** — 프로세스는
#   정상으로 뜨고 주입 로그도 찍히는데 **데이터가 한 톨도 안 흐른다**(C 실측: gateway 컨테이너
#   에서 S11 RUN → 시뮬 자식 정상, Kafka 미도달). 오늘 만난 실패들과 같은 모양이라 env 로 뚫는다.
#   ⚠️ 키 이름이 서비스마다 갈린다 (C 실측 · #130 compose · #136 리뷰): agent-service·spc-consumer·
#   prediction-sink = `KAFKA_BOOTSTRAP_SERVERS` / **gateway·grouper = `KAFKA_BOOTSTRAP`**.
#   이 코드가 실제로 도는 곳은 gateway 라 **2순위가 현행 정본**이다 — 둘 다 보는 이 폴백이라
#   어느 키가 지워져도 하나는 남는다. 한 줄로 줄이면 컨테이너 안에서 localhost = 자기 자신이 되어
#   이 PR 이 고친 증상(프로세스 정상·로그 정상·데이터 0)이 그대로 돌아온다.
DEFAULT_BOOTSTRAP = (os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
                     or os.environ.get("KAFKA_BOOTSTRAP")     # gateway·grouper 정본 (+ run_stack.ps1)
                     or "localhost:9092")
TOPIC_RAW = "fdc.raw"
DEFAULT_LOCK_PORT = 47651   # 단일 기동 락 전용(127.0.0.1) — 정본은 params.yaml
                            # simulator.single_instance.port. 여기 값은 키 부재 시 폴백
SIM_EVENTS_PATH = ROOT / "sim_events.jsonl"  # 답안지 (CWD 무관 고정 경로)


def run(args) -> None:
    """메인 루프 — 멀티챔버 리플레이 + 시나리오 주입 + 발행."""
    params = load_params()
    sim_cfg = params["simulator"]
    chamber_count = args.chambers or sim_cfg["chamber_count"]          # E1
    offset_sigma = sim_cfg["chamber_offset_sigma"]                      # E3
    noise_sigma = sim_cfg["chamber_noise_sigma"]                        # E3-보조
    label_delay = sim_cfg["label_delay"]["approx_wafers_fullscale"]     # E6

    log.info(f"📂 CSV 로딩: {args.csv}")
    df_full = pd.read_csv(args.csv)
    log.info(f"   → {len(df_full):,}행 / wafer {df_full['C64'].nunique():,}장")

    # replay seg1 필터 (2026-07-28): 발행은 리셋 후만, std·offset은 전체 유지(재현성·B 시딩 불변)
    df = (filter_post_reset(df_full)
          if sim_cfg.get("replay_post_reset_only", False) else df_full)
    replicator = ChamberReplicator(df, chamber_count, offset_sigma,
                                   noise_sigma=noise_sigma, stats_df=df_full)

    injector = None
    if args.scenario:
        scenarios = [load_scenario(Path(p)) for p in args.scenario]
        injector = ScenarioInjector(scenarios, replicator._std, std_lookup=replicator.local_std,
                                    events_path=SIM_EVENTS_PATH)
        log.info("💉 시나리오 장전: " + ", ".join(s["scenario_id"] for s in scenarios))

    # M3 ② 상시프로세스 라이브 주입 (파일드롭) — --scenario 없이도 빈 injector로 대기.
    # S11 RUN이 control/inject/에 yaml을 드롭하면 다음 wafer에서 inject_now (잔류 승계 §0-2).
    ctl = None
    ic_cfg = sim_cfg.get("inject_control") or {}
    if args.inject_control or ic_cfg.get("enabled", False):
        if injector is None:
            injector = ScenarioInjector([], replicator._std, std_lookup=replicator.local_std, events_path=SIM_EVENTS_PATH)
        ctl = InjectControl(ROOT / ic_cfg.get("dir", "control/inject"), injector,
                            min_interval_sec=float(ic_cfg.get("min_interval_sec", 1.0)))
        log.info(f"🎚  라이브 주입 대기: {ctl.control_dir} "
                 f"(min_interval={ctl.min_interval}s)")

    producer = Producer({
        "bootstrap.servers": args.bootstrap,
        "client.id": "fdc-simulator",
        "linger.ms": 10,
        "batch.size": 65536,
        "compression.type": "lz4",
    })
    actual_pub = ActualPublisher(producer, label_delay_wafers=label_delay,
                                 enabled=True)  # 계약 v4.6 확정·컨펌 완료 → 활성화 (P4-6, 2026-07-13)
    qual_pub = QualPublisher(replicator, injector, params,
                             enabled=not args.no_qual)  # P5-3 선행 (7/10)

    # fdc.correction(recipe) 구독 — 승인된 튜닝을 setpoint에 반영 (P5-3 선행, v4.5)
    recipe_applier = None
    correction_consumer = None
    if not args.no_recipe_apply:
        from confluent_kafka import Consumer
        recipe_applier = RecipeApplier(params, events_path=SIM_EVENTS_PATH)
        correction_consumer = Consumer({
            "bootstrap.servers": args.bootstrap,
            "group.id": CONSUMER_GROUP,           # consumer-group-simulator (계약)
            "auto.offset.reset": "latest",
        })
        correction_consumer.subscribe([TOPIC_CORRECTION])
        log.info(f"🎛  {TOPIC_CORRECTION} 구독 (recipe만 처리, D6 ±{recipe_applier.delta_max_pct}%)")

    # graceful shutdown (헌법 6-2)
    stop = {"flag": False}

    def _shutdown(signum, _frame):
        log.info(f"⏹  신호 수신({signum}) — 정리 후 종료")
        stop["flag"] = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(f"🔌 Kafka {args.bootstrap} → {TOPIC_RAW} │ 챔버 {chamber_count}대 │ "
             f"delay={args.delay}s │ loop={args.loop}")

    sent_wafers = 0
    sent_rows = 0
    idx_by_chamber: dict = {}          # M3 ② — 라이브 주입 start 스탬프 소스
    # RTD 자동 정지 신호 (헌법 1-1 예외 4) — **쓰는 쪽과 같은 방식으로 경로를 정한다.**
    #
    # 🔴 왜 env 를 보는가 (2026-08-12). 신호를 쓰는 쪽은
    #   `orchestrator/chamber_inhibit.py:41` 에서
    #       INHIBIT_DIR = Path(os.environ.get("RTD_INHIBIT_DIR", "") or REPO_ROOT/"control"/"inhibit")
    #   인데 읽는 쪽(여기)은 `ROOT/control/inhibit` 로 **하드코딩**돼 있었다. 지금은 둘 다
    #   env 가 없어 우연히 같은 경로지만, **누가 `RTD_INHIBIT_DIR` 을 넣는 순간 조용히 갈라진다** —
    #   쓰는 쪽은 "정지 성공"으로 기록하고(`chamber_inhibits` INSERT), 읽는 쪽은 파일을 못 찾아
    #   **생산을 계속한다.** 에러가 없어 화면상 정상과 구분이 안 되고, 감사에는 "정지됨"이
    #   남는다(사람은 멈췄다고 믿는다 — 헌법 7장 *"파일을 썼으니 조치가 적용됐다고 간주"* 계열).
    #   실제로 2026-08-07 에 같은 형태를 겪었다(grouper 가 볼륨 없는 컨테이너 내부에 신호를
    #   **성공적으로** 씀). 배포지가 늘수록 이 갈림의 확률만 커지므로 지금 맞춰 둔다.
    inhibit_dir = Path(os.environ.get("RTD_INHIBIT_DIR", "") or (ROOT / "control" / "inhibit"))
    log.info(f"RTD inhibit 감시 경로: {inhibit_dir}")   # 갈렸을 때 로그 한 줄로 잡히게
    inhibited_state: dict = {}                      # chamber → bool (전이 로그용)
    for chamber_id, wafer, ch_wafer_idx, pm_count in replicator.stream_wafers(loop=args.loop):
        if stop["flag"] or (args.limit and sent_wafers >= args.limit):
            break

        # RTD 챔버 inhibit — 신호 파일 존재 시 해당 챔버 생산 중단 (skip). 해제 = 파일 제거
        # (requal 승인 하류 — orchestrator.chamber_inhibit.release 가 유일한 제거 주체).
        # 다른 챔버는 계속 생산 (챔버 단위 정지 — 멘토 확정 'RP 생산 조절' 정합).
        _inh = (inhibit_dir / f"{chamber_id}.json").exists()
        if _inh != inhibited_state.get(chamber_id, False):
            inhibited_state[chamber_id] = _inh
            log.warning(f"{'■ 생산 중단' if _inh else '□ 생산 재개'} — {chamber_id} "
                        f"(RTD inhibit {'발효' if _inh else '해제'}, idx {ch_wafer_idx})")
        if _inh:
            # 2026-08-10 정지 중 계측 통과 (PM) — 정지가 Qual 까지 막으면 해제가 불가능해진다.
            #   해제는 requal 승인 하류(R9)가 유일한 제거 주체인데, R9 는 Qual 판정을 전제하고,
            #   Qual 은 이 스트림에서 PM 경계(pm_count 증가)를 관측해야 발행된다. 여기서 통째로
            #   continue 하면 [정지 → Qual 불가 → 판정 불가 → R9 불가 → 해제 불가] 데드락 —
            #   8/10 자동 정지 첫 실발동에서 실측했다. 실팹 의미론도 같다: **정지된 챔버에도
            #   계측(Qual) 웨이퍼는 흐른다** — 재가동 판정이 그 계측을 전제하므로.
            #   그래서 생산 wafer 행·fdc.actual 발행은 계속 막고(생산 중단 유지), 아래 세 개만
            #   통과시킨다: 주입 폴링(BM 시나리오 장전) · PM 관측(pm_bumps — 일반 경로와 동일
            #   계산) · Qual 5장 발행. sent_wafers 도 세지 않는다 — 생산이 아니다.
            idx_by_chamber[chamber_id] = ch_wafer_idx
            if ctl is not None:
                ctl.poll(idx_by_chamber)
            if injector is not None:
                pm_count += injector.pm_bumps(chamber_id, ch_wafer_idx)
            for qmsg in qual_pub.on_wafer(chamber_id, wafer, ch_wafer_idx, pm_count):
                producer.produce(topic=TOPIC_RAW,
                                 key=chamber_id.encode("utf-8"),
                                 value=json.dumps(qmsg, ensure_ascii=False).encode("utf-8"))
                producer.poll(0)
            continue

        # 라이브 주입 폴링 — 드롭 파일을 현재 순번으로 장전 (효과는 이 wafer부터)
        idx_by_chamber[chamber_id] = ch_wafer_idx
        if ctl is not None:
            ctl.poll(idx_by_chamber)

        # 가상 PM(pm_reset 시나리오) 가산 — pm_count 증가가 QualPublisher를 자동 트리거 (P5-3)
        if injector is not None:
            pm_count += injector.pm_bumps(chamber_id, ch_wafer_idx)

        # 승인된 recipe 보정 수신 (비차단 — wafer 1장마다 1회 poll)
        if correction_consumer is not None:
            poll_corrections(correction_consumer, recipe_applier)
        if recipe_applier is not None:
            recipe_applier.on_wafer(chamber_id)   # BL8 target 서보 램프 전진

        # 개방 감지 시 Qual 5장 선발행 (생산 재개 전 — 사이클_정의 Qual 실행 스펙)
        for qmsg in qual_pub.on_wafer(chamber_id, wafer, ch_wafer_idx, pm_count):
            producer.produce(topic=TOPIC_RAW,
                             key=chamber_id.encode("utf-8"),   # 파티션 키=chamber — 순서 보장 (B 요청 2026-07-21, 계약 §1)
                             value=json.dumps(qmsg, ensure_ascii=False).encode("utf-8"))
            producer.poll(0)

        orig_id = str(wafer["C64"].iloc[0])
        wafer_id = f"{orig_id}_{chamber_id.replace('SIM_', '')}"  # 예: C64_516_CH_3 (계약 §1 접미사 규칙)

        step_counter: dict = {}
        for _, row in wafer.iterrows():
            step = int(row["C7"]) if pd.notna(row["C7"]) else -1
            step_counter[step] = step_counter.get(step, 0) + 1
            msg = build_message(chamber_id, row, step_counter[step], pm_count,
                                replicator, injector, ch_wafer_idx, wafer_id,
                                recipe_applier=recipe_applier)
            producer.produce(
                topic=TOPIC_RAW,
                # 파티션 키=chamber (구 wafer_id) — 챔버=파티션=순서 보장. backlog 소비 시
                # 파티션 인터리빙으로 도착 순서 역전 → Nelson 정상 wafer 드롭 실측 65% 해소 (B 요청 2026-07-21)
                key=chamber_id.encode("utf-8"),
                value=json.dumps(msg, ensure_ascii=False).encode("utf-8"),
            )
            producer.poll(0)
            sent_rows += 1
            if args.delay > 0:
                time.sleep(args.delay)

        # wafer 완료 훅 — 실측 지연 발행 (스텁, 기본 off — fdc.actual 계약 초안)
        c65 = None
        if "C65" in wafer.columns and pd.notna(wafer["C65"].iloc[0]):
            c65 = float(wafer["C65"].iloc[0])
            # pm_reset 요란 전환의 라벨 이동 — actual 경유만 (raw 미포함, 헌법 1-3)
            if injector is not None:
                c65 += injector.y_delta(chamber_id, ch_wafer_idx)
        actual_pub.on_wafer_complete(wafer_id, str(wafer["C20"].iloc[0]),
                                     chamber_id, c65, pm_count)

        sent_wafers += 1
        if sent_wafers % 100 == 0:
            log.info(f"📤 wafer {sent_wafers:,}장 / row {sent_rows:,}건 "
                     f"(마지막: {wafer_id}, pm_count={pm_count})")

    producer.flush()
    if correction_consumer is not None:
        correction_consumer.close()
    log.info(f"✅ 종료 — wafer {sent_wafers:,}장 / row {sent_rows:,}건 발행")


def main():
    """CLI 진입점."""
    p = argparse.ArgumentParser(description="FDC 멀티챔버 시뮬레이터 (P3-1)")
    p.add_argument("--bootstrap", default=DEFAULT_BOOTSTRAP)
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    p.add_argument("--chambers", type=int, default=None,
                   help="가상 챔버 수 (기본: params.yaml E1)")
    p.add_argument("--scenario", action="append", default=None,
                   help="시나리오 YAML 경로 (반복 지정 가능)")
    p.add_argument("--delay", type=float, default=0.1,
                   help="row 간 간격(초), 0=최대 속도")
    p.add_argument("--limit", type=int, default=None, help="최대 wafer 수")
    p.add_argument("--loop", action="store_true", help="무한 리플레이")
    p.add_argument("--no-qual", action="store_true",
                   help="개방 후 Qual 5장 자동 발행 비활성 (기본: 활성)")
    p.add_argument("--no-recipe-apply", action="store_true",
                   help="fdc.correction(recipe) 구독→setpoint 반영 비활성 (기본: 활성)")
    p.add_argument("--inject-control", action="store_true",
                   help="파일드롭 라이브 주입 강제 활성 (기본: params.yaml inject_control.enabled)")
    args = p.parse_args()

    if not args.csv.exists():
        log.error(f"❌ CSV 없음: {args.csv}")
        sys.exit(1)

    # 중복 기동 차단 (2026-08-07) — producer 2개면 파티션 도착 순서가 뒤집혀
    # SPC 단조성 가드가 전량 드롭하고 적재가 조용히 멎는다. 상세 = single_instance.
    # 러너(_proc)가 아니라 producer 자신이 잠가야 CLI·게이트웨이 재시작 두 경로가 다 막힌다.
    si_cfg = (load_params().get("simulator") or {}).get("single_instance") or {}
    if si_cfg.get("enabled", True):
        try:
            single_instance.acquire(
                port=int(si_cfg.get("port", DEFAULT_LOCK_PORT)),
                name="시뮬레이터 producer",
                pid_hint_path=ROOT / "control" / "simulator_producer.pid",
            )
        except single_instance.AlreadyRunning as e:
            log.error(f"❌ {e}")
            sys.exit(2)

    run(args)


if __name__ == "__main__":
    main()
