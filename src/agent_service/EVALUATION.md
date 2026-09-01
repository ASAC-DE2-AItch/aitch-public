# Agent Service 평가 방법 (Supervisor·tool 판단 품질 측정)

> 이 문서는 통합 Agent Service의 **판단 품질을 재는 실험 절차**를 정의한다.
> 코드 정합(pass/fail)은 `pytest`가 보고, **LLM 판단이 좋은가**는 여기 절차로 잰다.
> 소유: 팀원 C. 관련 함정 = `docs/adr/TSR-0001`, CLAUDE.md 7장.

---

## 빠른 시작 — 라운드 한 번 돌리기 (EC2 꺼진 상태에서)

> ⚠️ **반드시 사용자 인터랙티브 터미널에서 foreground 실행** (백그라운드 금지 — §5·TSR-0001).

```powershell
cd C:\Users\Dell3571\Documents\final\AITCH-agent_service

# 1) 서버 기동 + 측정 (EC2 꺼져 있으면 여기서 스팟 부팅 ~20분). -KeepServer = 이어서 더 돌리려고 안 내림
.\src\agent_service\scripts\run_round.ps1 -Tag fewshot2 -KeepServer

# 2) 프롬프트 고쳐 또 측정할 땐 — 부팅 없이 측정만 (~7분, 서버 살아있는 동안 반복)
$env:PYTHONIOENCODING="utf-8"
python -m src.agent_service.eval_supervisor --real --tag fewshot3 --concurrency 2

# 3) 추이 분석 (LLM 재호출 없음 — 언제든)
python -m src.agent_service.analyze_quality --tag fewshot2

# 4) 다 끝났으면 GPU 내림 (원클릭)
.\src\agent_service\infra\llm-ec2-experiment\teardown_llm.ps1
```
- 진행 관찰(딴 창): `Get-Content .\src\agent_service\notes\eval\supervisor_results_fewshot2.jsonl -Wait -Tail 3`
- **1회만 돌리고 끝**이면 `-KeepServer` 빼면 측정 후 자동 destroy 된다.
- 상세·함정은 아래 §3~§5.

---

## 0. 왜 pytest가 아닌가

| | pytest | 평가 하네스 |
|---|---|---|
| 재는 것 | 내 코드가 맞나 (이분법·결정적) | LLM 판단이 좋나 (점수·분포·흔들림) |
| 실패 시 | 빌드 차단 | 차단 안 함 — 프롬프트 튜닝 신호 |

**LLM 판정은 pass/fail로 못 잰다.** "36건 중 22건 맞음, 노후를 정비로 7번 착각"처럼 **분포**로 봐야 프롬프트를 어디 고칠지 안다.

---

## 1. 지표 3축

| 축 | 질문 | 도구 |
|---|---|---|
| ① 판정 | 맞았나 (정답률·혼동행렬·판정별) | `eval_supervisor.py` |
| ② 오답의 방향 | 틀렸을 때 **어디로** (되돌릴 수 있는 쪽인가) | `analyze_quality.py` |
| ③ 근거의 질 | **맞아도** 제대로 맞았나 (정답옵션이 확신 1위였나·카드 있었나) | `analyze_quality.py` |

- **② 가역성**: 비용 크기가 아니라 **되돌릴 수 있는가** 한 축. 불가역(정비) / 가역(레시피 ±3%·관리선 0.5σ) / 무개입(이관). ⚠️ "위험도"가 아님 — 아는 것만 주장한다.
- **③ 근거의 질**: "정답옵션 conf 1위 아님", "카드 0장으로 맞힘", "manual 비변별 근거" 등 *맞아도 이상한* 케이스를 잡는다.
- ② 프레임은 멘토 확인 축이다(계산은 근거 확보용).

---

## 2. 도구 2종

### `eval_supervisor.py` — 판정 채점 하네스 (①)
정답지 = **fixture 파일명**(`01_baseline_aging_below_gate.json` → 기대 verdict=baseline_aging).

```powershell
$env:PYTHONIOENCODING="utf-8"
python -m src.agent_service.eval_supervisor --real --tag <라운드명> --concurrency 2   # 실 LLM (EC2 필요)
python -m src.agent_service.eval_supervisor                                          # Mock (배관만 검증·품질 아님)
python -m src.agent_service.eval_supervisor --read --tag <라운드명> --only-wrong      # 오판정 서술 읽기 (LLM 재호출 없음)
```
결과 → `notes/eval/supervisor_results_<tag>.jsonl` (**실제 서술 보존** — 숫자만으론 근거 설득력을 못 본다).

### `analyze_quality.py` — 사후 분석기 (②③)
저장된 jsonl을 읽어 ②③을 **재계산**한다. LLM 재호출 없음 → EC2 쿼터 0이어도 언제든 돈다.

```powershell
python -m src.agent_service.analyze_quality --tag <라운드명>     # 가역성 혼동행렬 + 근거의 질
```
`eval_supervisor.score()`(①)는 건드리지 않는다(CI 게이트라 안전 유지). ②③은 검증되면 접거나 멘토 확인 후 편입.

---

## 3. 라운드 1회 실행 — `scripts/run_round.ps1`

도커(Postgres·Qdrant) → GPU(EC2 스팟) → 측정 → **자동 destroy**를 한 번에. 순서를 기억에 안 맡긴다.

```powershell
# ⚠️ 반드시 사용자 인터랙티브 터미널에서 (foreground). 백그라운드/detached 금지 — 아래 §5.
cd C:\Users\Dell3571\Documents\final\AITCH-agent_service
.\src\agent_service\scripts\run_round.ps1 -Tag <라운드명>          # 온디맨드 기본, 끝나면 자동 destroy
.\src\agent_service\scripts\run_round.ps1 -Tag <라운드명> -Spot        # 스팟 (비용 우선 · 회수 시 라운드 무효)
.\src\agent_service\scripts\run_round.ps1 -Tag <라운드명> -KeepServer  # 연속 측정 (요금 주의)
```
- 부팅 ~20분(35B AWQ 적재+CUDA 컴파일) + 측정 ~10분. BootTimeout 1800초.
- 진행 관찰(딴 창): `Get-Content .\src\agent_service\notes\eval\supervisor_results_<tag>.jsonl -Wait -Tail 3`
- ✅ **G 온디맨드 쿼터 증설 승인 완료**(2026-07-22, `L-DB2E81BA` = 8 vCPU) → **기본이 온디맨드**로 바뀌었다(2026-07-31).
  4 vCPU 인스턴스 기준 **동시 1대**까지 — 남이 띄워둔 게 있으면 `VcpuLimitExceeded`.
- ⚠️ **destroy가 boot timeout으로 스킵되면 인스턴스가 남는다** — 수동 회수:
  `cd src\agent_service\infra\llm-ec2-experiment; terraform destroy -auto-approve`

### 여러 라운드 = 부팅 1번 (튜닝 세션 권장)
매 라운드 destroy→apply 하면 20분 부팅이 반복된다. 서버를 **살려두고 재활용**한다:
```powershell
# 1) 한 번만 — 기동 + 첫 측정 (destroy 안 함)
.\src\agent_service\scripts\run_round.ps1 -Tag r1 -KeepServer
# 2) 이후 — 부팅 없이 측정만 (run_round 불필요, .env 가 이미 그 서버를 가리킴 · ~7분/라운드)
$env:PYTHONIOENCODING="utf-8"
python -m src.agent_service.eval_supervisor --real --tag r2 --concurrency 2
# 3) 세션 종료 — 원클릭 내림 (destroy + .env 로컬 복귀)
.\src\agent_service\infra\llm-ec2-experiment\teardown_llm.ps1
```
- keep-alive 는 라운드 사이에도 GPU 과금된다 — 자리 비우면 `teardown_llm.ps1`.
- destroy 는 기본이 opt-out(`-KeepServer`)이다. 기본을 keep 으로 뒤집지 않는다(깜빡 누수 방지).

---

## 4. 튜닝 루프 (프롬프트·few-shot 실험 순서)

1. **기준선 확보** — 현재 라운드 측정 → `analyze_quality`로 ②③ 기록 (비교 기준).
2. **한 번에 한 변수만** 바꾼다 — 예: maintenance tool에 few-shot 대조쌍 주입. (동시 여러 개 바꾸면 원인 분리 불가.)
   - 프롬프트 정본 = `docs/Agent_프롬프트_라이브러리_v1.md` → **문서 먼저 개정 후 코드 동기화**(헌법 4-3).
3. **재측정** → 새 tag로 라운드.
4. **추이 비교** — 목표 지표만 보지 말고 **분포 전체**를 본다:
   - 목표가 좋아졌나? / 오답이 **다른 판정으로 이사**갔나? / confidence 분포가 어떻게 변했나?
   - 국소 개입은 전체 분포를 옮긴다. 한 셀만 보면 속는다 — 혼동행렬로 확인.

---

## 5. 측정 무효 함정 (조용히 실패하는 것들)

측정은 **"성공"으로 끝나도 무효**일 수 있다. 시작 전 재료가 흐르는지부터 본다.

| 함정 | 증상 | 방지 |
|---|---|---|
| 도커 미기동 | Qdrant 없음 → 근거 카드 3도구 다 0장, 판정도 무너짐 (에러 없음) | `run_round.ps1`이 step 1에서 Qdrant 컬렉션 검문 |
| **detached 실행 시 임베딩 DLL 차단** | "DLL load failed importing _C: 앱 제어 정책 차단" → kb_hits 전부 빔 | **foreground 전용** — 백그라운드로 안 띄운다 (`TSR-0001`) |
| 스팟 회수 | 마지막 N건 fallback으로 기록 | `_preflight`가 시작 전 엔드포인트 검문 |

- `eval_supervisor`의 `_preflight`/`_preflight_data`가 LLM·Qdrant·Postgres 생존을 검문한다.
- 후속(미구현): `_preflight_data`에 `embed_hybrid(['ping'])` 스모크 1회 추가 — detached 임베딩 차단 조기 감지.

---

## 6. 파일 지도

| 경로 | 내용 | git |
|---|---|---|
| `eval_supervisor.py` · `analyze_quality.py` | 하네스·분석기 | 추적 |
| `scripts/run_round.ps1` | 라운드 자동화 (UTF-8 **with BOM**) | 추적 |
| `fixtures/alerts/*.json` | 정답지 (파일명=기대 verdict) | 추적 |
| `notes/eval/supervisor_results_<tag>.jsonl` | 라운드 결과 (서술 포함) | **미추적** |
| `notes/eval/runs_history.jsonl` | 라운드 추이 누적 | **미추적** |
| `notes/jsonl_viewer.html` | 결과 뷰어 (jsonl 드롭) | **미추적** |

> 결과 jsonl은 미추적이라 팀 공유는 요약을 PR 본문·`docs/`로 꺼낸다.
