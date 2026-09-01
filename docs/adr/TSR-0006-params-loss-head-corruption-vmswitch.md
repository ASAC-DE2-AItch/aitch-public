# TSR-0006: 대시보드 OFFLINE 하나에서 사고 3건 연쇄 — 워킹트리 params 유실 · `.git/HEAD` NUL 손상 · VmSwitch 붕괴

## Status

Resolved (미해결 4건 이관 — 아래 Consequences)

## Context

- **발생일**: 2026-08-11 심야 ~ 2026-08-12 새벽
- **담당자**: PM
- **관련 컴포넌트**: `src/gateway/`, `config/params.yaml`, `docker-compose.yml`, git 워킹트리, 호스트(Docker Desktop/WSL2)
- **증상**: 대시보드 상단 OFFLINE. 시작은 한 줄이었는데 추적하며 서로 무관한 사고 3건이 겹쳐 있던 것이 드러났다.

세 사고는 **원인이 전혀 다른데 증상이 같은 자리로 모였다**. 그래서 하나를 고칠 때마다 "아직도 안 된다"가 반복됐다.

---

### 사고 ① — 워킹트리 `params.yaml` 에서 최상위 블록 3개 유실

```
File "/app/src/gateway/main.py", line 40, in <module>
    SSE_INTERVAL_SEC = PARAMS["operations"]["dashboard_poll_sec"]  # E5
KeyError: 'operations'
```

`docker ps` → `fdc-gateway   Restarting (1) 12 seconds ago` (무한 루프).

**왜 크래시 루프인가**: 그 줄은 함수 안이 아니라 **모듈 레벨**이다. import 시점에 터지므로 uvicorn 이 앱 객체를 만들지도 못하고 죽고, `restart: unless-stopped` 가 성실히 되살린다. 요청을 한 건도 안 받으니 액세스 로그도 없다.

| | 줄 수 | `operations` | `retrieval` | `stack` |
|---|---:|---|---|---|
| HEAD(`62c5da6`) 판 | 704 | ✅ | ✅ | ✅ |
| 워킹트리 | 648 | ❌ | ❌ | ❌ |

compose 가 `./:/app` **바인드 마운트**라 컨테이너는 호스트 워킹트리 파일을 직접 읽는다 → **커밋되지 않은 워킹트리 변경이 그대로 런타임 장애**가 됐다.

**같은 유실인데 증상이 갈렸다** — 이 사고에서 가장 오래 헤맨 지점:

```python
src/gateway/main.py:40      PARAMS["operations"]["dashboard_poll_sec"]   # 하드 서브스크립트 → import 즉시 크래시
src/gateway/stack_runner.py:44  (...).get("stack") or {}).get("services") or []   # → 조용히 빈 목록
```

`stack` 쪽은 예외도 경고도 없이 빈 목록이 되므로, 세 블록이 함께 사라졌는데도 **한 블록만 사라진 것처럼 보였다.**

### 사고 ② — `.git/HEAD` 가 NUL 로 패딩돼 git 이 멎음

```
$ git log --oneline
fatal: your current branch appears to be broken

$ git status -sb
## A  .coderabbit.yaml
A  .dockerignore
A  CLAUDE.md
...                      ← 전 파일이 'A'(added) 로 표시
```

**전 파일 `A` 를 데이터 유실로 오인하기 쉽다.** 실제로는 HEAD 해석 실패로 git 이 **빈 트리와 비교**한 것뿐이고, 인덱스·오브젝트는 멀쩡했다.

```
$ xxd .git/HEAD
00000000: 7265 663a 2072 6566 732f 6865 6164 732f  ref: refs/heads/
00000010: 6465 760a 0000 0000 0000 0000 0000 0000  dev.............
00000020: 0000 0000 0000 0000 0000 00              ...........
                     ↑ 정상 20 bytes + NUL 23 bytes = 43 bytes
```

NTFS 가 비정상 종료 시 **파일 크기는 복구하고 내용은 0 으로 채우는** 전형적 흔적. `refs/heads/dev`(41B)·커밋 오브젝트·reflog 는 전부 정상이라 **데이터 유실 0**.

> ⚠️ **같은 파일을 git 버전마다 다르게 읽었다.** Windows git 은 트레일링 NUL 을 무시하고 `rev-parse --abbrev-ref HEAD` → `dev` 를 정상 반환했고, 다른 git 은 `fatal` 을 냈다. "내 쪽에선 되는데"가 환경 차이가 아니라 **손상 허용도 차이**였다.

### 사고 ③ — Hyper-V VmSwitch 붕괴로 인프라 3종 동시 사망, 그리고 **복구되지 않음**

```
02:39:26   Kernel-Power 105    ← 전원 공급원 변경 (AC ↔ 배터리)
02:39:44   nhi 9008            ← Thunderbolt 컨트롤러 이벤트
02:39:56   nhi 9007
03:03:16 ~ 03:03:27   Microsoft-Windows-Hyper-V-VmSwitch  (232/233/234 · 67/69/71 · 102 · 291/292)

03:03:30.255360402Z   fdc-kafka     exit 255
03:03:30.263302003Z   fdc-qdrant    exit 255
03:03:30.263387687Z   fdc-postgres  exit 255
```

서로 의존하지 않는 컨테이너 셋이 **8ms 안에** 죽었다 → 애플리케이션 원인 배제. WSL2/Docker VM 이 가상 스위치 재생성 과정에서 네트워크를 잃은 것으로 보인다.

**문제는 붕괴가 아니라 복구 부재다.**

```
$ docker inspect ... --format "{{.Name}} restart={{.HostConfig.RestartPolicy.Name}}"
/fdc-postgres      restart=no            ← 죽은 채 16분 방치
/fdc-kafka         restart=no
/fdc-qdrant        restart=no
/fdc-spc-consumer  restart=on-failure    ← :3 소진 후 영구 포기
```

정책이 있던 `gateway`(`unless-stopped`)만 혼자 살아남았다. 나머지는 사람이 `docker start` 로 살렸다.

---

## Decision

| 사고 | 조치 |
|---|---|
| ① | `git checkout HEAD -- config/params.yaml` 로 원복 (704줄). HEAD 판이 정본이라 **커밋·PR 불필요** |
| ② | `.git/HEAD` 를 20바이트로 재작성. `refs/heads/dev` 가 온전했으므로 복구는 이 한 파일뿐 |
| ③ | `docker-compose.yml` 에 `postgres`·`kafka`·`qdrant` `restart: unless-stopped` 신설(PR #177) + 실행 중 컨테이너에 `docker update --restart` 선반영 |

**`always` 가 아니라 `unless-stopped`** 인 이유: `demo_reset.py` 가 의도적으로 멈추는 컨테이너를 되살리면 안 된다. `gateway` 와 같은 정책으로 맞췄다.

**`kafka-init` 의 `restart: "no"` 는 건드리지 않았다** — 토픽 생성 원샷 잡이라 재시작을 걸면 무한 루프가 된다. 같은 이유로 `ct0-bias-updater` 는 `unless-stopped` → **`on-failure`** 로 바꿨다(mode=off 기동 거부는 *정상 종료*라 되살리면 안 되는데, 밤새 62초마다 `pip install` 을 다시 도는 루프였다).

### 기각한 대안

- **① 을 `operations:` 블록만 다시 써서 수습** — 실제로 처음엔 그렇게 고쳤다. 그러나 유실은 세 블록이었고, HEAD 판이 온전한 정본이므로 **부분 패치는 반쪽 상태를 고착**시킨다. 원복으로 전환.
- **③ 을 `docker compose up -d` 로 수습** — 컨테이너 재생성은 볼륨·상태 리스크가 있고 헌법 3-2 의 `down -v` 금지 취지와도 멀다. `docker start` + `docker update` 로 재생성 없이 처리.

---

## 복구 과정이 만든 2차 사고 (이 TSR 에서 가장 값어치 있는 부분)

**1. 진행 중이던 `git revert` 를 확인하지 않고 커밋했다 → `--abort` 탈출구 소실**

```
$ git switch -c fix/params-operations-restore
fatal: cannot switch branch while reverting        ← 정확히 경고했다
$ git commit -m "..." -- config/params.yaml        ← PowerShell 이 다음 줄을 그대로 실행
[dev 034c135] ... 1 file changed, 39 insertions(+), 89 deletions(-)
```

넣은 건 6줄인데 **89줄이 삭제**됐다 — 반쯤 진행된 revert 상태가 통째로 커밋된 것이다. 미푸시라 `git reset --soft HEAD~1` 로 철회했지만, **커밋이 revert 를 종료시켜 `REVERT_HEAD` 가 사라졌고 `git revert --abort` 를 쓸 수 없게 됐다.**

> PowerShell 은 네이티브 명령이 실패해도 다음 줄을 계속 실행한다. **실패해야 멈추는 절차는 `if ($LASTEXITCODE -ne 0) { return }` 를 명시**하거나 한 줄씩 확인하며 진행해야 한다.

**2. `git commit -- <path>` 는 index 는 지켜주지만 그 파일 자체의 기존 워킹트리 변경은 못 막는다**

`docker-compose.yml` 커밋에서 10줄을 예상했는데 18줄이 나왔다. 차이 8줄은 8/11 에 작업되고 **커밋되지 않은 채 워킹트리에만 있던** gateway `docker.sock` 마운트 + `COMPOSE_PROJECT_NAME` 이었다.

역설적으로 **그게 구조였다** — 사고 ① 에서 우리가 `git checkout HEAD -- config/params.yaml` 을 했듯이, 누가 같은 명령을 `docker-compose.yml` 에 했으면 그 두 줄은 조용히 사라졌고 회차 초기화 버튼이 `[Errno 2] 'docker'` 로 다시 죽었을 것이다.

**3. 도구가 보는 파일 상태를 검증 없이 신뢰해 두 번 오판했다**

보조 환경(샌드박스 마운트)이 `.git/` 에 대해 stale 하고 자기모순인 값을 돌려줬다 — 같은 `ls` 안에서 `REVERT_HEAD` 를 목록에 띄우면서 동시에 "No such file" 을 냈고, 호스트가 고친 `HEAD` 를 계속 손상판으로 보여줬다. 그걸 근거로 ⓐ "HEAD 가 아직 안 고쳐졌다" ⓑ "누가 이미 복구를 시도한 흔적이 있다"(유령) 두 번 틀린 결론을 냈다.

**신선도 테스트를 자기가 쓴 파일로 해서 성립하지 않았다** — 내가 쓴 파일은 당연히 fresh 하게 보인다. 전파를 검증하려면 **반대편이 쓴 파일**을 봐야 한다.

---

## Consequences

- **해결 결과**: 게이트웨이 200/0.08s · 인프라 4종 healthy · `dev` 정상 해석 · `origin/dev` 무변경 · 재시작 정책 신설(런타임 선반영 완료)
- **트레이드오프**:
  - 바인드 마운트(`./:/app`)는 데모 편의를 위해 유지한다. 대가로 **커밋되지 않은 워킹트리 변경이 곧 런타임 구성**이며, 사고 ① 은 그 구조에서 나온 첫 사례다.
  - `unless-stopped` 는 진짜 크래시도 되살린다. 인프라 3종은 상태 저장 서비스라 가용성을 택했으나, 애플리케이션 컨슈머(`spc-consumer`·`a-pred`)는 실패 가시성을 위해 `on-failure:3` 을 **유지**했다(아래 미해결).
- **재발 방지**: CLAUDE.md 7장에 4항 추가 (import-time config 접근 / 워킹트리 유실 / 실패해도 계속 도는 스크립트 / 도구가 보는 상태 신뢰)
- **미해결 사항**:
  1. ~~**중단된 revert 의 정체**~~ → **해소 (2026-08-12 09:0x)**: revert 는 **프론트 작업(#170 계열, −4,642줄)을 되돌리는 중**이었다 — 발의 경위 미상(오조작 추정). 정체가 늦게 드러난 이유: 구 프론트 컨테이너가 revert 이전 번들을 서빙해 화면상 정상으로 보였고, 리빌드가 revert 잔재를 굽자 "작업 소실"로 나타났다. 수습 = `git stash push -- frontend`(잔재 격리, `wip/frontend-20260812` 태그 이중 박제) → frontend HEAD 원복 → 버튼 배선 2파일 재적용 → 리빌드. 밤 WIP 4파일(QualScreen S7Brief 등 +442)은 `stash@{0}` 별도 보존 — 낮 선택 적용.
  2. ~~**`apply_target('manual_option')` 이 아직 `event='WaferDispositioned'`**~~ → **해소 (2026-08-12 같은 날)**: `MaintenanceApproved`(fdc.agent 라우팅)로 개명. `WaferDispositioned` 는 처분 확정 전용으로 환원. 계약 §6 병기 + 회귀 테스트 3파일(route·payload·이벤트 목록) 동반.
  3. **`spc-consumer`·`a-pred` 의 `on-failure:3`** — 시연 안정성 vs 실패 가시성 판단 대기.
  4. **전원·네트워크 고정** — 사고 ③ 의 방아쇠(도킹/전원 변경)를 8/13 시연 전에 봉쇄할 것.
