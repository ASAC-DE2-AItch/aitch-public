# TSR-0002: 닫힌 승인 thread 재개가 200으로 통과 — 예외 기반 가드의 사각 (유령 성공)

## Status

Resolved

## Context

- **발생일**: 2026-07-30 (진단·재현 = 팀원 C / 원인 코드·수정 = PM)
- **담당자**: PM 윤준호 (게이트웨이 소유), 진단 프로브 팀원 C (팀원 C — PR #71)
- **관련 컴포넌트**: `src/gateway/main.py::approvals_decide`, `src/gateway/approvals.py::resume_decision`, LangGraph `Command(resume)` / `interrupt()`
- **증상**: 엔지니어가 **이미 결정이 끝난 Incident** 에 두 번째 결정(예: 반려)을 보내면 —

  ```
  보낸 것:  action=reject   approver=SECOND
  받은 것:  action=approve  approver=FIRST     ← 1차 승인의 상태가 그대로 반환
  소요:     48.7ms  (타임아웃이 아니라 즉시 정상 반환)
  기록:     approval_records 1건 → 1건 (무변화)
  ```

  HTTP 200 이 나가고 응답 모양·상태코드·소요시간이 정상 승인과 구별되지 않는다. **DB 를 직접 보지 않으면 실패를 알 수 없다.** 누른 사람은 반려했다고 믿는데 감사 추적에는 그 사실이 없다 — 헌법 1-1(승인 상태머신 통과 + `approval_records` 기록)이 조용히 깨진다.

- **왜 늦게 발견됐나**: 이 버그는 **에러가 나지 않는 것이 증상**이다. 로그·응답·지연 어디에도 신호가 없어, 정상 동작과 구별할 관측 지점이 존재하지 않았다.

## Decision

### 근본 원인 — 가드가 예외에 의존했다

기존 가드는 `try: resume_decision(...) except Exception: 409` 한 겹이었고, 전제는 "재개할 PENDING 이 없으면 예외가 난다" 였다. **LangGraph 는 재개 지점(`get_state().next`)이 없는 thread 에 `Command(resume)` 을 보내도 예외를 내지 않고 조용히 통과하며 저장된 최종 상태를 그대로 돌려준다.**

PENDING 없는 incident 가 두 종류인데 한쪽만 새는 것이 혼선을 키웠다:

| 케이스 | thread 상태 | `resume` 결과 | 라우트 |
|---|---|---|---|
| A. 그래프 미시작 | 상태 없음 | `KeyError: 'incident_id'` | ✅ 409 (**우연히** 걸림) |
| B. 승인 완료로 닫힘 | 상태 온전(values 9개) | 예외 없음 | 🔴 200 · 기록 무변화 |

A 가 우연히 잡히는 바람에 "가드가 있다"고 오인됐다. 실제로는 **상태가 온전할수록 안 걸리는** 가드였다.

### 채택 해결 — 예외가 아니라 상태로 판정, 두 겹

① **DB 가드 (1차)** — 열린 승인 요청(`approval_records` PENDING)이 없으면 재개할 대상 자체가 없으므로 409. #68 에서 CT² 라우팅용으로 넣어둔 `SELECT request_type ... WHERE status='PENDING'` 이 이미 같은 정보를 가져오고 있었으므로 **추가 왕복 0**. 조회 실패(`None`)와 "행 없음"(`False`)을 센티넬로 구분해, DB 장애가 정상 승인을 막지 않게 한다(fail-open).

② **상태 가드 (2차)** — `approvals.has_resume_point()` 신설. `get_state(cfg).next` 가 비면 409. DB 는 PENDING 인데 thread 는 닫힌 불일치를 거르고, **파생 thread(`<incident_id>-CT2`, §8-E)까지 한 규칙으로** 덮는다. 크래시 후 재개 지점이 남은 thread 는 `next` 가 비지 않으므로 통과 — 재개가 정당한 경우를 막지 않는다.

기존 `except → 409` 는 남긴다(3중 방어). 조회 자체가 실패하는 경우의 판정 보류를 뒤에서 받는다.

- **기각한 대안 ①**: `resume_decision` 반환값에서 결정 반영 여부를 사후 검증 — 반환 상태가 "1차 승인의 정상 상태"라 정오답 구분이 불가능(보낸 값과 대조하려면 노드 내부 규약에 의존).
- **기각한 대안 ②**: DB 가드만 — 파생 thread·체크포인터 불일치를 못 잡고, PENDING 행 유무와 그래프 상태가 어긋나는 순간(크래시·수동 DB 조작)에 다시 샌다.

## Consequences

- **해결 결과**: 케이스 A·B 모두 409. 정상 경로(열린 PENDING + 재개 지점 존재)는 그대로 통과. 라우트가 그래프를 건드리기 전에 걸러 불필요한 체크포인터 왕복도 사라진다.
- **회귀 방어**: `tests/test_ghost_success_guard.py` 신설. A절은 실 LangGraph + MemorySaver 로 `has_resume_point` 의미를 4상태(미접촉·대기·닫힘·조회실패)에서 고정하고, **"닫힌 thread 재개가 예외 없이 통과한다"는 버그의 전제 자체를 단언**한다 — LangGraph 가 이 동작을 바꾸면 이 테스트가 먼저 깨져 재검토 신호가 된다. B절은 라우트 가드를 fake conn 으로 검증(langgraph 불요). **이 신호가 실제로 울리려면 CI 가 의존성을 갖고 있어야 한다** — `.github/workflows/tests.yml` 에 `fastapi`·`langgraph`·`psycopg2-binary` 를 함께 넣었다(안 넣으면 8건 중 7건이 조용히 skip 되어 가드가 이름만 남는다, 2026-08-02 C 리뷰).
- **실사용 경로였다**: 추론이 아니다. S6 Disposition 화면이 5초 폴링으로 목록을 갱신하는데 사용자 선택(`sel`)이 남아, 이미 확정된 incident 에 승인을 다시 보내면 정확히 케이스 B 다 (PR #67 `selIncident` 지적과 같은 자리 — 그쪽은 `5cb693b` 로 별도 수정).
- **트레이드오프**: 라우트가 매 결정마다 `get_state` 를 1회 더 호출한다. 체크포인터 조회 1회(수 ms)이고 승인은 사람이 누르는 저빈도 경로라 무시 가능.
- **재발 방지**: CLAUDE.md 7장 한 줄 추가 — "예외가 안 났으니 성공"으로 간주하지 말 것.
- **미해결 사항**: 승인 API 에 **동시성 가드가 없다.** 두 엔지니어가 같은 Incident 를 동시에 결정하면 DB 가드를 둘 다 통과할 수 있다(check-then-act). 실제 이중 기록은 `commit_decision` 의 `WHERE status='PENDING'` + `rowcount == 0 → False` 가 막지만, **진 쪽에게도 200 이 나가는** 같은 종류의 거짓 신호가 남는다. 후속: 커밋 결과를 라우트 응답에 반영(반영 실패 시 409).

## 참고

- 진단 프로브 원본: `src/agent_service/scripts/ct2_resume_probe.py`, `gateway_ghost_success_probe.py` (PR #71, 팀원 C). 실 DB 대조는 이쪽이 정본이고, CI 회귀는 위 테스트가 담당한다.
- 관련: PR #67(S6 stale 선택 — 이 경로를 실제로 만들어낸 화면 버그), 헌법 1-1·1-4, 계약 §8-E(모델 게이트 파생 thread).
