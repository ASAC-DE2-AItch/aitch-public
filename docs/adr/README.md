# docs/adr/ — ADR·TSR 인덱스

> **ADR**(Architecture Decision Record)과 **TSR**(Trouble Shooting Record)을 통합 관리합니다.
> 새 문서 작성 시 아래 인덱스에 한 줄 추가하세요. 근거: 헌법 8장.

## 접두사 규칙

| 접두사 | 용도 | 예시 |
|---|---|---|
| `ADR-NNNN` | 아키텍처 결정 기록 | fdc.alert 단일 토픽 원칙 채택 이유 |
| `TSR-NNNN` | 트러블슈팅 기록 | Consumer OOM 원인 및 해결 |

## 인덱스

| 번호 | 제목 | 종류 | Status | 날짜 | 담당 |
|---|---|---|---|---|---|
| TSR-0001 | 실 LLM 평가 라운드 detached 실행 시 임베딩 DLL 차단 → 측정 무효 | TSR | Resolved | 2026-07-23 | 팀원 C |
| ADR-0001 | pm_log 챔버 스코프 — 단기 전역 유지, 챔버별 필터는 후속 전환(학습·서빙 동시 + 동결 벤치 재검증 전제) | ADR | Accepted (후속 Proposed) | 2026-07-28 | 팀원 A·B |
| TSR-0002 | 닫힌 승인 thread 재개가 200으로 통과 — 예외 기반 가드의 사각(유령 성공) | TSR | Resolved | 2026-07-30 | PM (진단 팀원 C) |
| TSR-0003 | 통합테스트 teardown 이 라이브 관리선 180행을 삭제 — 가드 경고를 끄는 방향으로 고쳐서 터졌다 | TSR | Resolved | 2026-08-09 | 팀원 B |
| TSR-0004 | 시뮬 producer 중복 기동 — 예외 없이 적재만 멎는다 (timestamp 역전 전량 드랍 · OS 뮤텍스로 봉쇄) | TSR | Resolved | 2026-08-07 (기록 08-09) | PM |
| TSR-0005 | Qual 승인 방치 → 전 챔버 PHASE_0 고착 — 28시간 주입 무음 폐기 (무음 세 겹 · 이관 3건) | TSR | Resolved (이관 Open) | 2026-08-09 | PM (이관 팀원 B) |
| TSR-0006 | 대시보드 OFFLINE 하나에서 사고 3건 연쇄 — 워킹트리 params 유실 · `.git/HEAD` NUL 손상 · VmSwitch 붕괴 (복구 중 2차 사고 3건 포함) | TSR | Resolved (미해결 4건) | 2026-08-12 | PM |

<!-- 새 문서 추가 시 위 표에 한 줄 append -->

## 운영 원칙

- **30분 이상 소요된 디버깅은 TSR 작성 의무** (헌법 8장).
- TSR 작성: `TSR-0000-템플릿.md` 복사 후 편집. ADR은 Nygard 표준 포맷 (Context / Decision / Consequences).
- Status `Investigating` → 해결 후 `Resolved` 업데이트 (번호 유지). 다른 원인으로 재발 시 새 TSR 발급 + 기존에 `Superseded by TSR-XXXX` 표기.
- TSR 작성과 헌법 7장(자주 하는 실수) 한 줄 추가는 세트.
- 월 1회 팀 회고 때 이 인덱스 리뷰.
