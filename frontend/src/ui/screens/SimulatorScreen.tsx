import { useEffect, useRef, useState } from "react";
import { scenarios as mockScenarios } from "../../mock/fdc";
import {
  demoReset, demoResetStatus, fetchScenarios, restartGateway, runScenario, simLogs, simStatus,
  stackStart, stackStatus, stackStop, stopScenario,
  type ComposeContainer, type DemoResetStatus, type SimScenario, type SimStatus,
  type StackService, type StackSnapshot,
} from "../../live/api";

/** S11 시뮬레이터 — 시나리오 실행 (가상 Fab 제어) + 주입 로그 (P6-3 실배선)
 *  게이트웨이 살아 있으면 RUN = 실제 kafka_producer 기동 (CLI와 동일 효과, 동시 1개 가드),
 *  오프라인이면 기존 목업 연출 폴백 — 시연 안전망. */

/** 시연 큐시트 순서 (발표 2026-08-13 · `시연_시나리오_대본_v1` §3) — 라이브러리를 **누를 순서**로
 *  정렬한다. 파일명 사전순이면 storm 이 drift 앞에 와서 실제 조작 순서와 어긋난다(실측 8/11).
 *  ④ Recipe 는 대본에서도 미확정(D-2) — 소재가 정해지면 여기 한 줄만 추가하면 자리를 찾는다. */
const CUE: Record<string, { n: number; label: string }> = {
  drift_c11:                { n: 1, label: "① T−3분 예열" },
  alarm_storm_ch2:          { n: 2, label: "② 0:00 프리롤" },
  qual_loud_ch2:            { n: 3, label: "③ 3:30 데모2" },
  multi_chamber_common_c17: { n: 5, label: "⑤ 9:00 데모3.5" },
};

/** yaml stem → 데모 태그 (시나리오 번호는 일정 v5 확정 매핑) */
const TAG: Record<string, string> = {
  drift_c11: "시나리오 1", qual_loud_ch4: "시나리오 2",
  alarm_storm_ch2: "엣지", hard_fault_spike_c31: "엣지",
  multi_chamber_common_c17: "엣지", oscillation_c62: "엣지", dryrun_drift_c11: "검증용",
  // s060·s085·s130·s155 = PM **강도 배율** 파생 (A26 프라이어 전이 원장 분산용, s 만 곱한다).
  // 시작 오프셋이 아니다 — 네 파일 모두 start_after_wafers 는 원본과 같다.
  // 태그를 안 주면 "pm_reset" 으로 뜨는데 시연 중에 본 시나리오와 구분이 안 된다.
  qual_loud_ch4_s060: "파생 s0.6", qual_loud_ch4_s085: "파생 s0.85",
  qual_loud_ch4_s130: "파생 s1.3", qual_loud_ch4_s155: "파생 s1.55",
};
const POLL_MS = 1500;

export default function SimulatorScreen() {
  // ---- 라이브 상태 ----
  const [live, setLive] = useState<boolean | null>(null);   // null=탐지 중
  const [scList, setScList] = useState<SimScenario[]>([]);
  const [st, setSt] = useState<SimStatus | null>(null);
  const [lines, setLines] = useState<string[]>([]);
  const [note, setNote] = useState<string>("");
  const pollRef = useRef<number | null>(null);

  // ---- 인프라 상태 (2026-08-10 개편) ----------------------------------------
  //   구: 호스트 자식 프로세스(StackRunner) 관리 패널. b-spc(#88)·a-pred·grouper(#130)·
  //   c-agent(#142) 가 차례로 compose 로 이관돼 `stack.services` 가 비었고, `[].every()` 가
  //   true 라 **0줄인데 "전체 가동 중" 초록불**이 켜지는 허위 신호가 됐다.
  //   신: 볼 대상을 compose 컨테이너로 바꾼다 — 이게 실제로 확인해야 하는 것이다.
  //   호스트 프로세스 목록은 비어 있지 않을 때만 그린다(되돌릴 경우 대비 · 코드 보존).
  const [snap, setSnap] = useState<StackSnapshot | null>(null);
  const [stackBusy, setStackBusy] = useState(false);
  const refreshStack = async () => setSnap(await stackStatus());
  useEffect(() => {
    refreshStack();
    const t = window.setInterval(refreshStack, 3000);
    return () => window.clearInterval(t);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const svcs: StackService[] = snap?.services ?? [];
  const conts: ComposeContainer[] = snap?.containers ?? [];
  const allUp = svcs.length > 0 && svcs.filter((x) => x.enabled).every((x) => x.running);
  const contBad = conts.filter((c) => !c.ok);

  // ---- 회차 초기화 (2026-08-10) — A-2 를 버튼으로. 2단 확인 후 실행 ----------
  const [rs, setRs] = useState<DemoResetStatus | null>(null);
  const [armed, setArmed] = useState(false);            // 1클릭=무장, 2클릭=실행 (오조작 방지)
  useEffect(() => {
    const pull = async () => setRs(await demoResetStatus());
    pull();
    const t = window.setInterval(pull, 2000);
    return () => window.clearInterval(t);
  }, []);
  const resetting = !!rs?.running;
  /* 초기화 로그 상자를 **항상 맨 아래로** 붙인다 (2026-08-13).
     상자가 위쪽을 보여주는 바람에, 완료된 뒤에도 화면엔 `── 1/6 시뮬레이터 정지` 가 그대로
     떠 있어 **1단계에서 멈춘 것처럼** 보였다 — `✅ 완료` 줄은 스크롤을 내려야 나왔다.
     실제로 "초기화가 진행이 안 된다"는 제보가 여기서 나왔다. */
  const resetLogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = resetLogRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [rs?.lines?.length, rs?.running]);
  const doReset = async () => {
    if (!armed) { setArmed(true); return; }
    setArmed(false);
    const r = await demoReset();
    if (r) setRs(r);
    await refreshStack();
  };

  // ---- 목업 폴백 상태 (기존 연출 보존) ----
  const [mockLog, setMockLog] = useState<{ t: string; id: string; name: string }[]>([]);
  const [mockRunning, setMockRunning] = useState<Set<string>>(new Set());

  const refresh = async () => {
    const [s, l] = await Promise.all([simStatus(), simLogs(60)]);
    if (s) setSt(s);
    if (l) setLines(l);
    return s;
  };

  useEffect(() => {
    (async () => {
      const sc = await fetchScenarios();
      if (sc) { setLive(true); setScList(sc); await refresh(); }
      else setLive(false);
    })();
    return () => { if (pollRef.current) window.clearInterval(pollRef.current); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // 실행 중일 때만 폴링
  useEffect(() => {
    if (!live) return;
    if (st?.running && pollRef.current == null) {
      pollRef.current = window.setInterval(async () => {
        const s = await refresh();
        if (s && !s.running && pollRef.current != null) {
          window.clearInterval(pollRef.current); pollRef.current = null;
        }
      }, POLL_MS);
    }
  }, [live, st?.running]); // eslint-disable-line react-hooks/exhaustive-deps

  const runLive = async (id: string) => {
    const r = await runScenario(id);
    setNote(r.detail);
    await refresh();
  };

  const runMock = (id: string, name: string) => {
    if (mockRunning.has(id)) return;
    const t = new Date(); const ts = `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
    setMockLog((l) => [{ t: ts, id, name }, ...l].slice(0, 8));
    setMockRunning((r) => new Set(r).add(id));
  };

  const runningId = st?.running ? st.scenario : null;

  return (
    // 2026-08-10 레이아웃: 카드 전부 한 화면에. 시나리오 11개 + 로그가 페이지를 밀어내려
    //   스크롤이 생겼고, 시연 중 스크롤은 관객이 길을 잃는다. 화면 높이를 그리드에 주고
    //   길어지는 두 카드(라이브러리·로그)만 **내부 스크롤**로 돌린다.
    //   전역 `.grid` 는 `flex:none` 이라 다른 화면에 영향 주지 않도록 인라인으로만 덮는다.
    <div className="scr" style={{ flex: 1, minHeight: 0 }}>
      <div className="scrh">
        <div>
          <div className="t">Simulator</div>
          <div className="c num">
            시나리오 주입 → 알람 → 승인 → 효과 검증 · GATE:{" "}
            <b style={{ color: live ? "var(--mint-deep)" : "var(--alarm)" }}>
              {live == null ? "…" : live ? "● LIVE (RUN = 실주입)" : "○ OFFLINE — 목업 연출"}
            </b>
            {note && <span style={{ marginLeft: 10, color: "var(--faint)" }}>{note}</span>}
          </div>
        </div>
        {live && st?.running && (
          <button className="gopill" onClick={async () => { await stopScenario(); await refresh(); }}>
            ■ STOP — 상시 세계 종료 (마지막 주입: {st.scenario})
          </button>
        )}
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1.3fr .7fr", flex: 1, minHeight: 0 }}>
        <div className="card" style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
          <div className="chead">
            <span className="t">시나리오 라이브러리</span>
            <span className="cap num">
              src/simulator/scenarios · YAML{live ? " · 실시간" : ""}
              {live && scList.length ? ` · ${scList.length}건` : ""}
            </span>
          </div>

          {/* 내부 스크롤 — 카드가 늘어나지 않게 flex:1 + minHeight:0 */}
          <div style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
          {live ? [...scList].sort((a, b) =>
              ((CUE[a.id]?.n ?? 99) - (CUE[b.id]?.n ?? 99)) || a.id.localeCompare(b.id)
            ).map((s) => {
            const isRun = runningId === s.id;
            const cue = CUE[s.id];
            const tag = cue?.label ?? TAG[s.id] ?? s.pattern ?? "—";
            return (
              <div className="srow" key={s.id}>
                <span className={"stag num" + (cue || tag.startsWith("시나리오") ? " main" : "")}>{tag}</span>
                <div className="qbd" style={{ flex: 1 }}>
                  <div className="qt">{s.scenario_id ?? s.id}</div>
                  <div className="qs">{s.id} · 대상 {(s.chambers ?? []).join("·") || "—"} · {s.error ?? s.description}</div>
                </div>
                <button
                  className={"runbtn num" + (isRun ? " live" : "")}
                  title={st?.running ? "실행 중 세계에 주입 — 앞 컷 효과는 잔류 (M3 ② 원복 금지)"
                                     : "상시 세계 기동 + 주입 (최초 RUN)"}
                  onClick={() => runLive(s.id)}
                >
                  {isRun ? "● 주입됨" : st?.running ? "주입 ▶" : "RUN ▶"}
                </button>
              </div>
            );
          }) : mockScenarios.map((s) => (
            <div className="srow" key={s.id}>
              <span className={"stag num" + (s.tag.startsWith("시나리오") ? " main" : "")}>{s.tag}</span>
              <div className="qbd" style={{ flex: 1 }}>
                <div className="qt">{s.name}</div>
                <div className="qs">{s.id} · 대상 {s.target} · {s.desc}</div>
              </div>
              <button className={"runbtn num" + (mockRunning.has(s.id) ? " live" : "")} onClick={() => runMock(s.id, s.name)}>
                {mockRunning.has(s.id) ? "● LIVE" : "RUN ▶"}
              </button>
            </div>
          ))}
          </div>
          <div className="scrfoot num" style={{ padding: "6px 16px 10px" }}>
            주입 순서(2026-08-10 실증): <b>시나리오 1 먼저 → 90초 → 시나리오 2</b>. 차가운 세계에
            pm_reset 을 먼저 넣으면 첫 관측에 PM 이 겹쳐 Qual 이 삼켜진다(랩 경계까지 18분 대기).
          </div>
        </div>

        <div className="col">
          {/* 회차 초기화 — 「준비까지 버튼만」의 마지막 조각. 관객 앞이 아니라 회차 사이에 쓴다. */}
          <div className="card">
            <div className="chead">
              <span className="t">회차 초기화</span>
              {/* 소요는 **실측값**을 적는다 — 구 "약 60초"는 실제(131~133초)의 절반이라
                  60초쯤에서 "안 끝난다"로 읽혔다 (2026-08-13 3회 실측). */}
              <span className="cap num">A-2 · 오프셋 latest · Qual 재시딩 · <b>약 2분</b></span>
            </div>
            <div className="krow num" style={{ fontSize: 10.5, color: "var(--faint)", lineHeight: 1.5 }}>
              운영 데이터만 비운다 — 관리선은 initial 로 원복, KB·모델 캐시는 건드리지 않는다.
            </div>
            {/* 진행 중이든 끝났든 로그는 남긴다 — 끝나고 나서 "몇 group×topic 이 리셋됐나"를
                확인해야 하는데, 구 버전은 완료되면 로그가 사라져 검증할 방법이 없었다.
                내부 스크롤이라 카드 높이는 안 늘어난다. */}
            {(rs?.lines?.length ?? 0) > 0 && (
              <>
                {resetting && (
                  <div className="krow">
                    <b className="num">{rs?.step ?? "…"}</b>
                    <span className="num" style={{ marginLeft: 8, fontSize: 10, color: "var(--faint)" }}>
                      {(rs?.step_i ?? 0) + 1}/{rs?.steps?.length ?? 6} · {rs?.elapsed_sec ?? 0}초
                    </span>
                  </div>
                )}
                {/* 끝난 뒤에도 결과를 한 줄로 못박는다 — 로그를 다 읽지 않아도 성패가 보이게. */}
                {!resetting && rs?.ok !== null && rs?.ok !== undefined && (
                  <div className="krow">
                    <b className="num" style={{ color: rs.ok ? "var(--blue-deep)" : "var(--pink)" }}>
                      {rs.ok ? "✅ 초기화 완료" : "⛔ 초기화 실패"}
                    </b>
                    <span className="num" style={{ marginLeft: 8, fontSize: 10, color: "var(--faint)" }}>
                      {rs.elapsed_sec ?? 0}초 · {rs.ok ? "[Simulator] 에서 RUN 을 눌러야 데이터가 흐릅니다" : "아래 로그 확인"}
                    </span>
                  </div>
                )}
                <div ref={resetLogRef} style={{ maxHeight: 132, overflowY: "auto" }}>
                  {(rs?.lines ?? []).map((l: string, i: number) => (
                    <div className="krow num" key={i} title={l} style={{ fontSize: 10, padding: "2px 16px",
                         overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{l}</div>
                  ))}
                </div>
              </>
            )}
            {resetting ? null : (
              <div style={{ padding: "4px 16px 12px" }}>
                <button className={armed ? "gopill" : "runbtn num"}
                        style={{ fontSize: 11.5 }}
                        disabled={!live}
                        title={live ? "운영 테이블 초기화 + Kafka 오프셋 latest + Qual 스냅샷 재시딩"
                                    : "게이트웨이 미접속 — 초기화 불가"}
                        onClick={doReset}>
                  {armed ? "정말 초기화 — 한 번 더 누르세요" : "회차 초기화 ↺"}
                </button>
                {armed && (
                  <button className="runbtn num" style={{ marginLeft: 6, fontSize: 11 }}
                          onClick={() => setArmed(false)}>취소</button>
                )}
                {rs?.ok === true && (
                  <div className="num" style={{ marginTop: 6, fontSize: 10.5, color: "var(--mint-deep)" }}>
                    ✅ {rs.detail || "초기화 완료"}
                  </div>
                )}
                {rs?.ok === false && (
                  <div className="num" style={{ marginTop: 6, fontSize: 10.5, color: "var(--pink)" }}>
                    ❌ 중단 — {rs.detail}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* 주입 로그 — 남는 높이를 다 먹고 내부 스크롤 (자동으로 아래로 붙지 않는다:
              시연 중 화면이 튀면 관객 시선이 끌린다. 최신은 아래, 스크롤은 손으로.) */}
          <div className="card" style={{ flex: 1, minHeight: 90, display: "flex", flexDirection: "column" }}>
            <div className="chead">
              <span className="t">주입 로그</span>
              <span className="cap num">{live ? "producer stdout · tail" : "sim_events (답안지 — 채점 전용)"}</span>
            </div>
            <div style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
            {live ? (
              lines.length === 0
                ? <div className="krow num" style={{ color: "var(--faint)" }}>실행 대기 — RUN을 누르면 여기로 스트림</div>
                : lines.map((l, i) => (
                  // title 로 전문을 남긴다 — 폭이 좁아 잘리므로 마우스만 올리면 다 보인다.
                  <div className="krow num" key={i} title={l} style={{ fontSize: 10, padding: "3px 16px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {l}
                  </div>
                ))
            ) : mockLog.map((l, i) => (
              <div className="krow" key={i}>
                <b className="num" style={{ width: 44, color: "var(--faint)" }}>{l.t}</b>
                <span>{l.name}</span>
                <span className="num" style={{ marginLeft: "auto", color: "var(--label)", fontSize: 10 }}>{l.id}</span>
              </div>
            ))}
            </div>
          </div>

          {/* 인프라 상태 — compose 컨테이너 (2026-08-10 개편, 읽기 전용).
              「인프라에 올린 서버 켜서 프론트 진입」이 우리 준비의 전부이므로, 그게 실제로
              올라왔는지 확인하는 자리가 화면에 있어야 한다. agent-service 가 죽으면 Brief 0건,
              spc-consumer 가 Restarting 이면 알람 0건인데 둘 다 '조용한 정상'처럼 보인다. */}
          <div className="card">
            <div className="chead">
              <span className="t">인프라 상태</span>
              <span className="cap num">
                docker compose · 3초 폴링
                {conts.length > 0 && (contBad.length === 0 ? " · 전부 정상" : ` · 이상 ${contBad.length}건`)}
              </span>
              {/* 게이트웨이 재시작 (2026-08-11) — 마운트 코드 리로드 원클릭. 자식 시뮬이 함께
                  죽으므로 시뮬 RUN 중에는 비활성(runbtn 회색). 복귀 3~8초 — 3초 폴링이 잡는다. */}
              <button className="runbtn num" disabled={!!st?.running || stackBusy}
                      title={st?.running ? "시뮬 RUN 중 — STOP 후 재시작 가능 (자식 시뮬 동반 종료)"
                                         : "gateway 프로세스 재기동 — 마운트된 최신 코드 반영"}
                      style={{ marginLeft: "auto",
                               opacity: st?.running || stackBusy ? 0.4 : 1 }}
                      onClick={async () => {
                        setStackBusy(true);
                        setNote("gateway 재시작 중 — 3~8초 뒤 복귀");
                        await restartGateway();
                        setTimeout(() => { setStackBusy(false); refreshStack(); }, 6000);
                      }}>
                ↻ gateway 재시작
              </button>
            </div>
            {snap == null ? (
              <div className="krow num" style={{ color: "var(--faint)" }}>게이트웨이 미접속 — uvicorn 기동 필요</div>
            ) : conts.length === 0 ? (
              <div className="krow num" style={{ color: "var(--faint)", fontSize: 10.5 }}>
                확인 불가 — docker 미기동이거나 compose 조회 실패
              </div>
            ) : (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, padding: "6px 16px 12px" }}>
                {conts.map((c) => (
                  <span key={c.service} className="num" title={`${c.name} · ${c.status || c.state}`}
                        style={{ display: "inline-flex", alignItems: "center", gap: 5,
                                 fontSize: 10.5, padding: "3px 8px", borderRadius: 999,
                                 border: "1px solid var(--hairline)",
                                 color: c.ok ? "var(--label)" : "var(--pink-deep)",
                                 background: c.ok ? "transparent" : "var(--pink-soft)" }}>
                    <span className="dot" style={{ width: 6, height: 6, background: c.ok ? "var(--mint)"
                          : c.state === "absent" ? "#c3cdd6" : "var(--alarm)" }} />
                    {c.service}
                    {!c.ok && <b style={{ fontWeight: 600 }}>{c.state}</b>}
                  </span>
                ))}
              </div>
            )}
            {/* 호스트 프로세스 목록은 비어 있지 않을 때만 — 되돌릴 경우를 위해 코드는 남긴다. */}
            {svcs.length > 0 && (
              <>
                {svcs.map((v) => (
                  <div className="krow" key={v.name}>
                    <span className="dot" style={{ width: 6, height: 6,
                      background: v.running ? "var(--mint)" : v.enabled ? "var(--alarm)" : "#c3cdd6" }} />
                    <b className="num">{v.name}</b>
                    <span className="num" style={{ marginLeft: 6, fontSize: 10, color: "var(--faint)" }}>
                      {v.running ? `pid ${v.pid} · ${Math.floor((v.uptime_sec ?? 0) / 60)}m` : v.enabled ? "정지" : "비활성"}
                    </span>
                    <span className="num" title={v.last_line ?? undefined}
                      style={{ marginLeft: "auto", color: "var(--label)", fontSize: 10,
                               maxWidth: 150, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {v.last_line ?? ""}
                    </span>
                    {v.enabled && (
                      <button className="runbtn num" style={{ marginLeft: 6, fontSize: 10, padding: "2px 8px" }}
                        disabled={stackBusy}
                        onClick={async () => {
                          setStackBusy(true);
                          await (v.running ? stackStop(v.name) : stackStart(v.name));
                          await refreshStack(); setStackBusy(false);
                        }}>
                        {v.running ? "■" : "▶"}
                      </button>
                    )}
                  </div>
                ))}
                <div className="krow" style={{ justifyContent: "flex-end", gap: 6 }}>
                  <button className="gopill num" style={{ fontSize: 11 }} disabled={stackBusy || allUp}
                    onClick={async () => { setStackBusy(true); await stackStart(); await refreshStack(); setStackBusy(false); }}>
                    {allUp ? "● 전체 가동 중" : "서비스 모두 시작 ▶"}
                  </button>
                  <button className="runbtn num" style={{ fontSize: 11 }} disabled={stackBusy}
                    onClick={async () => { setStackBusy(true); await stackStop(); await refreshStack(); setStackBusy(false); }}>
                    모두 정지
                  </button>
                </div>
              </>
            )}
          </div>
          {/* 발행 채널 — **설정값**이다. 구 버전은 5줄 전부 초록 LED 였는데 그건 조회한 상태가
              아니라 하드코딩이라, 관객이 '실시간 헬스체크'로 오해할 수 있었다(라이브 상태는 위
              인프라 카드가 본다). 라벨을 '설정'으로 바꾸고 칩 한 줄로 눕혀 높이를 돌려준다. */}
          <div className="card">
            <div className="chead">
              <span className="t">발행 채널</span>
              <span className="cap num">계약 설정값 · 상태 아님</span>
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, padding: "6px 16px 12px" }}>
              {[
                ["fdc.raw", "4 챔버 · ETCH-01 · 라운드로빈"],
                ["fdc.prediction", "A 파이프라인"],
                ["fdc.alert", "B4-1 · mock 오프"],
                ["fdc.actual", "label_delay 13,900장 · Qual 미발행"],
                ["QUAL", "개방 후 5장 · pm_reset 연동"],
              ].map(([k, d]) => (
                <span key={k} className="num" title={d}
                      style={{ fontSize: 10.5, padding: "3px 8px", borderRadius: 999,
                               border: "1px solid var(--hairline)", color: "var(--label)" }}>
                  {k}
                </span>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
