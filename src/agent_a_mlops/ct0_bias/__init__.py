# -*- coding: utf-8 -*-
"""CT⓪ Model R2R — lean-85 예측 bias 자동 보정 (설계 정본 `docs/CT0_ModelR2R_설계방향_v1.md`).

패키지 구성 (설계 §11):
    core.py        순수 함수만 (I/O 0) — 산식·clamp·자격·양자화
    control_io.py  bias 파일 원자 교체·가드 로드 (updater → serving 단방향 채널)
    bootstrap.py   DB 전량 재계산 (기동·리밸런스·`--rebuild` CLI)
    updater.py     consumer 프로세스 (`fdc.actual` + `fdc.agent`, group=consumer-group-bias)

⚠️ 이 `__init__` 은 **의존성을 끌지 않는다**. `consumer.py`(서빙)는 `control_io` 만 import
하는데, 여기서 `updater`(confluent_kafka·psycopg2)를 끌어오면 서빙이 보정 경로의 의존성에
묶인다 — 설계 D3(장애 격리)의 정면 위반이다.
"""
