# =============================================================================
# markers.py   —  [담당: B]  합성 마커 rule_id 단일 소스
# =============================================================================
# `fdc.alert` 계약 §3 의 `violations` 는 `min_length=1` 이라 빈 배열이 불가능하다.
# 그래서 센서 SPC 위반이 아닌 판정(B9 crazy wafer 등)도 **합성 마커 위반 1건**을
# 그 자리에 끼워 발행한다 — `rule_id` 는 Nelson 룰이 아니라 **알람 종류 표지**다.
#
# 이 모듈이 존재하는 이유: 같은 문자열을 발행측(`publisher`)과 채점측(`axes/score_spc`)이
# 각자 들고 있으면, 마커 규격이 바뀔 때 한쪽만 고쳐도 **예외가 안 난다** — 채점측은
# 그냥 "미등록 rule_id" 로 흘려보낸다(헌법 7장 "폐기·신설하고 하류를 안 따라감").
# `axes/` 는 `config` 만 의존하는 leaf 라 `publisher` 를 역으로 import 할 수 없어,
# 양쪽이 함께 읽을 수 있는 leaf 모듈로 분리한다.
#
# ⚠️ **크로스파트 주의** — 같은 판별식이 다른 파트에도 있다 (헌법 3-1 소유 분리라 여기서
#   통합 불가). 마커 규격을 바꾸면 아래 세 곳이 **함께** 바뀌어야 한다:
#     · B   `src/common/context_score/markers.py`            (이 파일 — B 측 단일 소스)
#     · C   `src/agent_service/app/tools/base.py`            `CRAZY_MARKER_RULE_ID`
#     · PM  `src/orchestrator/incident_grouper.py`           `is_crazy_alert()`
#   C 주석의 "마커 규격이 바뀌면 두 곳이 함께 바뀌어야 한다(B 회신 합의, 2026-07-28)" 와 같은 계약.
# =============================================================================
from __future__ import annotations

# B9 = crazy wafer (predicted_c65 > P99 또는 anomaly 컷 초과) 종류 표지.
#   sensor='C65'(예측 타겟·센서 아님) · control_limit_*(관리선 아님) 를 동반한다.
CRAZY_MARKER_RULE_ID = "B9"

# `rule_base`(Nelson 룰 배점표) 조회 대상에서 제외되는 rule_id 집합.
#   Nelson 룰이 아니므로 e1(룰 기본점)을 주지 않는 것이 **의도**다 — 미등록이 아니다.
#   ⚠️ 여기 있는 id 를 `context_score_rule_base` 에 등재하지 말 것 (e1 이중계상).
MARKER_RULE_IDS: frozenset = frozenset({CRAZY_MARKER_RULE_ID})
