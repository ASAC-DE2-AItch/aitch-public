# TSR-0001: 실 LLM 평가 라운드가 detached(백그라운드) 실행 시 임베딩 DLL 차단으로 무효화

## Status

Resolved

## Context

- **발생일**: 2026-07-23
- **담당자**: 팀원 C (팀원 C)
- **관련 컴포넌트**: agent_service 평가 하네스(`run_round.ps1` → `eval_supervisor.py` → `vectordb.embed_hybrid` / FlagEmbedding bge-m3), Windows WDAC(앱 제어 정책)
- **증상**: maintenance few-shot 효과를 측정하려 `run_round.ps1 -Tag fewshot1` 을 **백그라운드(detached) 프로세스로 실행**. EC2·vLLM·Qdrant preflight 는 전부 통과했는데, KB 근거 검색 단계에서 매 alert 마다 아래가 떠 **kb_hits/cases 가 전부 빈 채로 진행**됨. 로그엔 치명 에러가 안 나 라운드는 "성공"으로 끝나지만 측정은 무효(모든 리포트가 KB 근거 0장 → maintenance 가 KB 미커버 경로로 쏠려 few-shot 효과와 구분 불가).

```
근거 검색 실패 — kb_hits/cases 없이 진행
(DLL load failed while importing _C: 애플리케이션 제어 정책에서 이 파일을 차단했습니다.)
```

## Decision

**근본 원인 = 패키지가 아니라 실행 컨텍스트.** 아래 3건이 전부 **foreground(인터랙티브/일반 도구 호출)에서는 정상**임을 확인:

```
python -c "import torch; torch.rand(1)"                          # OK (인터랙티브)
python -c "from FlagEmbedding import BGEM3FlagModel"             # OK (foreground)
python -c "from src.agent_service import vectordb; vectordb.embed_hybrid(['test'])"  # OK — dense dim 1024
```

실패한 유일한 경우가 **detached 백그라운드 프로세스**였다. 에러 문구("애플리케이션 제어 정책에서 차단")는 Windows WDAC/앱제어 정책의 차단이며, 이 정책은 detached 프로세스에 다른 무결성/토큰 컨텍스트를 적용해 torch·FlagEmbedding 의 네이티브 확장(`_C`) DLL 로드를 막는다. 참고: 이 머신은 유사한 DLL 차단 이력 있음(pandas 3.0.4 DLL 차단 — 3.0.3 고정).

**채택 해결**: 실 LLM 라운드(임베딩·torch 사용)는 **사용자 인터랙티브 터미널에서 foreground 로 실행**한다. 자동화 도구의 백그라운드/detached 실행 금지.

- 기각한 대안 ①: torch/FlagEmbedding 버전 다운그레이드 — foreground 에선 멀쩡하므로 패키지 문제가 아님(오진).
- 기각한 대안 ②: WDAC 정책에 DLL 예외 등록 — 머신 보안 정책 변경은 범위 밖·부작용 큼.

## Consequences

- **해결 결과**: `run_round.ps1 -Tag fewshot1` 을 인터랙티브 터미널에서 재실행하면 KB 임베딩 정상 → 정상 측정. fewshot1 무효분은 폐기(비교 금지).
- **트레이드오프**: 실측 라운드는 사람이 터미널에서 띄워야 함(에이전트가 백그라운드로 대신 못 돌림). ~30분 잡이라 진행은 `Get-Content ...supervisor_results_fewshot1.jsonl -Wait -Tail 3` 로 관찰.
- **재발 방지**: CLAUDE.md 7장 한 줄 추가 (torch/임베딩 실측 라운드 = foreground 전용).
- **미해결 사항**: preflight 가 "Qdrant 살아있음"만 검문하고 **클라이언트 측 임베딩 생존**은 안 봄 — detached 차단을 조기에 못 잡았다. 후속: `_preflight_data()` 에 `embed_hybrid(['ping'])` 스모크 1회 추가 검토(측정 시작 전 임베딩이 죽어 있으면 하드 스톱).
