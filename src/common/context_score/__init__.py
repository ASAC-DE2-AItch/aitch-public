# =============================================================================
# src/common/context_score/  —  Context Score 공용 모듈 (패키지 마커)
# =============================================================================
# 계획서: src/common/ContextScore/Docs/B4-1_공용구현_계획서.md
#
# [이 패키지의 목적]
#   Context Score 로직을 공용 모듈에 두고, 점수 산출을 "축별 순수 서브함수"로
#   쪼갠다. 각 멤버는 자기 축 함수 파일만 편집하므로 같은 브랜치에서도 서로
#   다른 파일을 건드려 머지 충돌이 없다.
#
#   context_score(joined):
#       s1 = score_spc(violations)      # B
#       s2 = score_tttm(tttm)           # B
#       s3 = score_ae(prediction)       # A
#       s4 = score_pred(prediction)     # A
#       return combine([s1, s2, s3, s4])   # 공동
#
# [경계 원칙]  (계획서 §1, §2)
#   - common 은 collector → score → publish "순수 로직"만 담는다.
#   - consumer(spc_consumer.py)는 여기 넣지 않는다. → src/agent_b_spc/ 에 잔류.
#     (이유: consumer 가 nelson/tttm 엔진을 import → common 에 두면 common→B 역의존)
#   - 의존 방향은 항상  agent_b_spc → common  (common 은 특정 멤버에 묶이지 않음).
#
# [작성 규칙]  (CLAUDE.md 발췌)
#   - 모든 함수에 docstring (6-1).
#   - 매직 넘버 금지 → config/params.yaml 경유, 근거는 config 합의안 문서 (6-4).
#   - print() 금지 → logging 사용 (6-1).
#   - 센서는 C코드(C11 등) 그대로 사용, 발표용 이름 하드코딩 금지 (6-4).
#   - 데이터 누수 금지: C65(타겟)를 feature 로 쓰지 않는다 (1-3).
#
# * 이 파일은 패키지 인식을 위한 마커다. 초기 세팅 단계에서는 비워 둔다
#   (필요 시 공개 심볼 re-export 는 구현 단계에서 합의 후 추가).
# =============================================================================
