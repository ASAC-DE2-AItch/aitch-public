import { useEffect, useMemo, useRef, useState } from "react";
import { briefs, incidentsAll, markerProposal, seedProposal, type Brief, type Incident } from "../../mock/fdc";
import { APPROVER, fetchEvidence, fetchPending, modifyGuard, postDecision, requestReanalysis,
         type DecisionPayload, type GateResult, type IncidentEvidence } from "../../live/api";

/** S4 승인 센터 — TypeBot형 가로 선택지 체인 (정의서 v1.6: 쓰기(resume)는 이 체인에서만) */
const VERDICT_COLOR: Record<string, string> = {
  equipment_fault: "var(--pink)", process_shift: "var(--mint)",
  baseline_aging: "var(--blue)", escalate: "var(--alarm)",
  wafer_disposition: "var(--amber, #d9a13b)",
};
const VERDICT_KO: Record<string, string> = {
  equipment_fault: "진짜 장비 이상", process_shift: "공정 조건 이탈",
  baseline_aging: "기준선 노후", escalate: "에스컬레이션",
  // 처분 라벨은 #175 승계 — 이 화면이 wafer_disposition 카드를 그린다.
  wafer_disposition: "웨이퍼 처분 심사",
};
/** A-warn 누적 표류 (2026-08-07) — gateway `fetch_pending` 이 동봉한 필드를 읽는다.
 *
 *  왜 필요한가: 엔진의 누적 가드(A3)는 reset_ref 기준이라 **승인할 때마다 기준이 갱신**돼
 *  누적이 리셋된다. 그래서 "매번 상한 안이었는데 총합으로는 크게 밀린" 상태를 아무도 못 본다
 *  (B 실측: C11 11회 승인 → 3.7σ). 이 배지가 그 총합을 승인 화면에 올린다.
 *
 *  **비차단**(헌법 1-1): 승인 버튼을 막지 않고 판단 재료만 준다. 값이 없으면(신규 그룹·σ 퇴화)
 *  배지도 없다 — 근거 없는 경고는 경고를 무디게 만든다. */
function cumDrift(row: any): { sigma: number; sensor: string | null; warned: boolean; thr: number | null } | null {
  const s = row?.cum_from_initial_sigma;
  if (typeof s !== "number") return null;
  return { sigma: s, sensor: row?.cum_worst_sensor ?? null,
           warned: !!row?.is_cum_warned, thr: row?.cum_warn_sigma ?? null };
}

/** 실 PENDING 행 + Brief 원문(original_value) — 목업 카드 앞에 병합 (P5-2b, 2026-07-24) */
type LiveInc = Incident & { live?: boolean; ov?: any; row?: any; cum?: ReturnType<typeof cumDrift> };

/** CT²(AE) 배포 승인 브리프 (2026-08-09 — 기준 개정 v2 §7).
 *
 *  이 화면은 SPC Incident(4지선다) 전용이라 CT² 배포 건은 `승인 대기 · <번들명>` 한 줄로만
 *  떴다. 승인자가 **무엇을 근거로 배포를 허락하는지** — 어떤 항목이 기본값보다 느슨한 채
 *  PASS 했는지(헌법 3-3 ③ ⓑ), 어떤 판정이 표본 부족으로 '확정'이 아닌지 — 를 볼 수 없었다.
 *
 *  ★문구는 여기서 만들지 않는다. gateway `attach_ct2_brief` 가 평탄화한 `ct2_brief` 는
 *  `validate_bundle.build_approval_brief` 산출 그대로다 — 게이트 항목·임계의 단일 소스가
 *  거기이므로 라벨도 거기서 온다. 여기서 키 이름을 번역하기 시작하면 임계를 바꿀 때
 *  두 곳을 고쳐야 하고, 안 고쳐도 아무 일도 안 일어난다 (헌법 7장). */
const isCt2 = (r: any) => r?.request_type === "ct2_deploy";

function Ct2Brief({ row }: { row: any }) {
  const b = row?.ct2_brief;
  if (!b) {
    return (
      <div className="ev counter" style={{ borderColor: "var(--alarm)" }}>
        <i className="num">주의</i>
        {row?.ct2_brief_missing ?? "게이트 브리프 없음 — 승인 근거 미상 (배포 보류 권고)"}
      </div>
    );
  }
  const risks: string[] = Array.isArray(b.residual_risks) ? b.residual_risks : [];
  return (
    <>
      <div className="verdict">
        <span className="vp" style={{ background: b.verdict === "PASS" ? "var(--mint)" : "var(--alarm)" }} />
        <b>CT²(AE) 번들 배포 — 게이트 {b.verdict}</b>
        <span className="num" style={{ color: "var(--faint)" }}>{row?.ct2_bundle ?? "—"}</span>
      </div>

      {/* 약화 조건 — "임계가 완화된 채 PASS"를 승인자가 보는 자리 (헌법 3-3 ③ ⓑ) */}
      <div className="vreason">
        약화 조건 <b>{b.weakened_count ?? 0}건</b> · 경계 라벨 <b>{b.boundary_count ?? 0}종</b>
      </div>
      {(Array.isArray(b.weakened) ? b.weakened : []).map((w: any, i: number) => (
        <div className="ev counter" key={`w${i}`}>
          <i className="num">약화</i>
          <b>{w.label}</b> — 기본 {String(w.baseline)} → 현재 {String(w.value)} · {w.why}
          {w.guard ? <><br />가드: {w.guard}</> : null}
        </div>
      ))}
      {(Array.isArray(b.boundaries) ? b.boundaries : []).map((x: any, i: number) => (
        <div className="ev" key={`b${i}`}>
          <i className="num">경계</i>
          {x.check} <b>{x.band}</b> — 관측 {((x.rate ?? 0) * 100).toFixed(2)}% ·
          {/* ★옵셔널 체이닝 필수 — 앱에 ErrorBoundary 가 없어 여기서 터지면 S4 가 통째로 백지가
              된다. 리포트 형태가 바뀌거나 손편집된 리포트가 들어와도 화면은 살아 있어야 한다. */}
          {" "}CI[{((x.ci?.[0] ?? 0) * 100).toFixed(2)}, {((x.ci?.[1] ?? 0) * 100).toFixed(2)}]% vs 예산 {(x.budget ?? 0) * 100}%
          {" — 표본이 작아 '예산 이내'가 확정이 아닙니다."}
        </div>
      ))}
      {risks.map((r, i) => (
        <div className="ev counter" key={`r${i}`}><i className="num">잔여</i>{r}</div>
      ))}
      {(row?.ct2_failed_checks ?? []).length > 0 && (
        <div className="ev counter" style={{ borderColor: "var(--alarm)" }}>
          <i className="num">FAIL</i>{(row.ct2_failed_checks as string[]).join(", ")}
        </div>
      )}
    </>
  );
}

/** 경로③ 죽은 구간(게이트웨이 pool 빌드, ~2026-07-31)의 PENDING 은 original_value 가 {} —
 *  fetch_pending 이 동봉한 agent_reports SUP 행(brief_*)으로 Brief 원문 형태를 재조립한다.
 *  (orchestrator fetch_bundle 과 대칭 — verdict 관례 매핑 포함) */
const OPT_VERDICT: Record<string, string> = {
  limit_option: "baseline_aging", recipe_option: "process_shift",
  manual_option: "equipment_fault", escalate: "escalate",
};
/** C 실 리포트의 evidence_cards[{evidence:[{source_type,snippet}]}] → 근거 문자열 배열 */
const cardLines = (o: any): string[] =>
  (o?.evidence_cards ?? []).flatMap((c: any) =>
    (c?.evidence ?? []).map((e: any) => `[${e.source_type}] ${e.snippet}`));
const briefFromRow = (r: any) => {
  if (!r || (r.brief_selected == null && r.brief_reason == null)) return undefined;
  const po = {
    recipe_option: r.brief_recipe_option ?? {},
    limit_option: r.brief_limit_option ?? {},
    manual_option: r.brief_manual_option ?? {},
  };
  const sel = (po as any)[String(r.brief_selected ?? "")] ?? {};
  return {
    parallel_options: po,
    supervisor_recommendation: {
      selected: r.brief_selected,
      // 정본 = supervisor_verdict/evidence/counter 컬럼 (2026-07-31 적재 신설). 구 행은 폴백.
      verdict: r.brief_verdict ?? sel.verdict ?? OPT_VERDICT[r.brief_selected ?? ""] ?? "escalate",
      reason: r.brief_reason,
      confidence: r.brief_confidence,
      evidence: (Array.isArray(r.brief_evidence) && r.brief_evidence.length
                 ? r.brief_evidence : cardLines(sel)),
      counter_evidence: r.brief_counter ?? sel?.evidence_cards?.[0]?.counter_evidence,
    },
  };
};

export default function ApprovalsScreen({ initialId }: { initialId: string | null }) {
  // P5-2b — 진짜 PENDING 병합: 5초 폴링, 실카드는 목업 앞에 (목업 유지 = 시연 안전망)
  const [liveRows, setLiveRows] = useState<any[]>([]);
  const refreshPending = async () => {
    const rows = await fetchPending();
    if (rows) setLiveRows(rows as any[]);
  };
  useEffect(() => {
    refreshPending();
    const t = setInterval(refreshPending, 5000);
    return () => clearInterval(t);
  }, []);

  const liveIncidents = useMemo<LiveInc[]>(() => liveRows.map((r) => {
    const ov = r.original_value ?? {};
    const sup = ov?.brief?.supervisor_recommendation ?? {};
    const ageMin = r.requested_at
      ? Math.max(0, Math.round((Date.now() - new Date(r.requested_at).getTime()) / 60000)) : 0;
    const isDispoRow = String(r.request_type ?? "") === "disposition";
    return {
      // 🔴 카드 키 = record_id (2026-08-11). incident_id 로 키를 잡으면 한 Incident 의
      //    정비 카드와 처분 카드가 **같은 키**가 되어, 목록의 앞선 하나만 열리고 나머지는
      //    클릭이 먹지 않는다(8/11 실측). API 호출은 아래 apiId(=행의 incident_id)로 한다.
      id: r.record_id != null ? `${r.incident_id}#${r.record_id}` : r.incident_id,
      chamber: String(r.chamber_id ?? "").replace(/^SIM_?CH_?/, "CH") || "—",
      sev: String(r.severity_max ?? "").toUpperCase() === "CRITICAL" ? "critical" : "warning",
      // 제목 우선순위: CT² 배포 > 경로③(DB 수합) Brief > original_value 내장 Brief > 폴백.
      // CT² 는 SPC Brief 가 없어 폴백까지 흘러 `승인 대기 · <번들명>` 한 줄이 됐다 —
      // 약화·경계 건수를 제목에 올려 목록에서부터 "그냥 PASS"와 구분되게 한다 (개정 v2 §7).
      // brief_reason 은 fetch_pending 이 agent_reports 최신 SUP 행에서 동봉 (2026-07-31).
      //   ⚠️ 처분 카드는 그 LATERAL(=최신 SUP Brief)을 **쓰면 안 된다** — 그건 장비 이상
      //   Brief 라 처분 카드 제목이 정비 사유로 뒤바뀐다(8/11). 카드 자신의 Brief 를 쓴다.
      //   우선순위 사슬(병합 8/11): 처분(가장 구체) → CT²(#164) → brief_reason → 폴백.
      title: isDispoRow
        ? `웨이퍼 처분 심사 — ${(ov?.wafer_disposition?.per_wafer ?? []).length}장 (${
            Object.entries((ov?.wafer_disposition?.counts ?? {}) as Record<string, number>)
              .map(([k, v]) => `${k} ${v}`).join(" · ") || "판정 대기"})`
        : (isCt2(r)
            ? `CT²(AE) 배포 — 게이트 ${r.ct2_verdict ?? "?"}`
              + (r.ct2_brief
                 ? ` · 약화 ${r.ct2_brief.weakened_count ?? 0}건 · 경계 ${r.ct2_brief.boundary_count ?? 0}종`
                 : " · ⚠ 브리프 없음")
            : undefined)
          ?? r.brief_reason ?? sup.reason
        ?? (r.brief_selected ? `${VERDICT_KO[OPT_VERDICT[r.brief_selected] ?? ""] ?? r.brief_selected} — 승인 대기`
        : ov.verdict ? `${VERDICT_KO[ov.verdict] ?? ov.verdict} — 승인 대기`
                     : `승인 대기 · ${r.selected_option ?? "리포트 수합 중"}`),
      alarms: 0, ageMin, status: "pending" as const, owner: null,
      // score = Context Score 만. priority_score 폴백 금지 — 정렬용 합성값(100 초과)이라
      // 점수 자리에 나오면 "score 2100" 오독이 난다 (2026-07-31 리허설 실측).
      score: Math.round(ov?.context_score ?? ov?.brief?.context_score ?? r.context_score ?? 0),
      live: true, ov, row: r, cum: cumDrift(r),
    } as LiveInc;
  }), [liveRows]);

  const open = useMemo<LiveInc[]>(
    () => [...liveIncidents,
           ...incidentsAll.filter((i) => ["pending", "analyzing", "reopen"].includes(i.status))],
    [liveIncidents],
  );
  const [selId, setSelId] = useState(initialId ?? open[0]?.id ?? "");
  const inc: LiveInc | undefined = open.find((i) => i.id === selId);
  /** 그래프/증거 API 는 **Incident ID** 로 부른다 — 카드 키(`INC#recordId`)와 다르다. */
  const apiId = String((inc as LiveInc | undefined)?.row?.incident_id ?? selId);
  /** initialId(= Incident ID) 해소 — **처분 카드 우선** (2026-08-11).
   *
   *  S6 의 [RELEASE로 요청]·[SCRAP으로 요청]·[권고대로 요청] 은 모두 처분 카드를 열고 이 화면으로
   *  넘긴다. 그런데 카드 키는 `INC#recordId` 라서 initialId 만으로는 못 찾고, 같은 Incident 의
   *  정비 카드가 목록에서 앞서 있어 그쪽이 잡혔다.
   *
   *  🔴 종전 구현은 effect 두 개(①처분 카드로 점프 ②키 불일치면 첫 후보로 폴백)였는데, ②가
   *  같은 커밋에서 **낡은 selId** 를 보고 ①이 방금 정한 처분 카드를 정비 카드로 되돌렸다
   *  (8/11 실측: "RELEASE로 요청 눌렀는데 다른 카드가 열림"). 하나로 합치고, 해소 결과를
   *  ref 에 기억해 **처분 카드가 나타나는 순간 한 번만** 이동한다 — 그 뒤 사용자의 카드 클릭은
   *  덮어쓰지 않는다(자동 이동이 수동 선택과 싸우면 안 된다). */
  const resolvedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!initialId) return;
    const cands = open.filter((i) => (i as LiveInc).row?.incident_id === initialId || i.id === initialId);
    if (!cands.length) return;                        // 아직 폴링 전 — 다음 라운드에 해소
    const dispo = cands.find((i) => String((i as LiveInc).row?.request_type ?? "") === "disposition");
    const key = `${initialId}:${dispo ? "dispo" : "first"}`;
    if (resolvedRef.current === key) return;          // 이 상태로는 이미 이동했다
    resolvedRef.current = key;
    setSelId((dispo ?? cands[0]).id);
  }, [initialId, open]);

  // 증거층 (2026-07-31 — PR #70). 서사(Brief)는 얼려두고 증거만 조회 시점에 다시 읽는다.
  // 5초 폴링이라 Brief 재생성 없이도 "지금 몇 건인지"가 계속 맞는다.
  const [evid, setEvid] = useState<IncidentEvidence | null>(null);
  const [reBusy, setReBusy] = useState(false);
  useEffect(() => {
    setEvid(null);
    if (!inc?.live) return;                       // 목업 카드는 증거층 없음 (실값 위장 금지)
    let stop = false;
    const pull = async () => {
      const e = await fetchEvidence(apiId);        // 카드 키가 아니라 Incident ID
      if (!stop && e) setEvid(e);
    };
    pull();
    const t = setInterval(pull, 5000);
    return () => { stop = true; clearInterval(t); };
  }, [selId, apiId, inc?.live]);
  const pullReanalysis = async () => {
    if (reBusy || !evid) return;
    setReBusy(true);
    const ok = await requestReanalysis(apiId);   // 〃
    if (ok) setEvid({ ...evid, needs_reanalysis: true });
    setReBusy(false);
  };

  // 실 Brief — original_value의 Brief 원문에서 변환 (없으면 목업 briefs 폴백)
  const liveBrief: Brief | undefined = useMemo(() => {
    const raw = inc?.ov?.brief ?? briefFromRow(inc?.row);   // 경로③ 죽은 구간 행 구제 (2026-07-31)
    const ov = inc?.ov;
    if (!raw?.supervisor_recommendation) return undefined;
    const sup = raw.supervisor_recommendation ?? {};
    const po = raw.parallel_options ?? {};
    const pick = (k: string) => po[`${k}_option`] ?? {};
    const selKeyRaw = String(sup.selected ?? ov?.selected_option ?? "").replace("_option", "");
    // none_executable(4지선다 ④)·미상 = 추천 없음 — 구 "limit" 강제 폴백이 가짜 '추천 1위'
    // 배지를 붙였다 (2026-07-31). undefined 면 배지·하이라이트 없이 전 옵션 기각 사유만 노출.
    const selKey = (["recipe", "limit", "manual"].includes(selKeyRaw) ? selKeyRaw : undefined) as Brief["recommended"];
    const selOpt = selKey ? pick(selKey) : {};
    const T: Record<string, string> = { limit: "실력치 재설정", recipe: "Recipe 튜닝", manual: "정비" };
    const v = String(ov?.verdict ?? sup.verdict ?? "baseline_aging");
    return {
      verdict: v as Brief["verdict"],
      verdictKo: VERDICT_KO[v] ?? v,
      reason: sup.reason ?? "—",
      confidence: Number(sup.confidence ?? 0),
      evidence: (selOpt.evidence ?? sup.evidence ?? cardLines(selOpt)).slice(0, 3),
      counter: selOpt.counter_evidence ?? sup.counter_evidence
        ?? selOpt?.evidence_cards?.[0]?.counter_evidence ?? "—",
      recommended: selKey,
      options: (["limit", "recipe", "manual"] as const).map((k) => ({
        key: k, title: T[k], action: pick(k).action ?? pick(k).rationale ?? "-",
        feasibility: pick(k).confidence != null ? `conf ${Number(pick(k).confidence).toFixed(2)}` : "—",
        confidence: Number(pick(k).confidence ?? 0),
        // 기각(흐림)은 **비선택 옵션의** rejected_because 만 (§4: 그 칸은 비선택 전용).
        // 선택된 옵션에 실려오면 LLM 이 유보 caveat 를 오배치한 것 — 기각으로 표시하면
        // "추천인데 기각" 모순 (실측 SUP-20260730-SIMCH4-0006 '가한계 Phase 대기').
        // escalate_reason 은 항상 유보성 비고(note) (2026-07-31).
        rejected: k === selKey ? undefined : pick(k).rejected_because,
        note: k === selKey ? (pick(k).rejected_because ?? pick(k).escalate_reason)
                           : pick(k).escalate_reason,
      })),
      dispo: { held: 0, rec: "RELEASE", reason: "—" },
    };
  }, [inc]);
  const brief = liveBrief ?? briefs[selId];
  // CT²(AE) 배포 건인가 — 02(브리프)·04(결정 2지선다)·확인 문구가 함께 분기한다.
  const ct2Row = isCt2((inc as LiveInc | undefined)?.row) ? (inc as LiveInc).row : null;
  // SYSTEM 제안 트랙 ①② (프라이어 — 모델 갱신 원칙 4호 준용, 승인 경유. 값 = 8/9 실측)
  const isProposal = selId === markerProposal.id;
  const P = markerProposal;
  const isSeed = selId === seedProposal.id;      // R9 시드 — 승인 시 POST /prior/seed 경로
  const S = seedProposal;
  const [opt, setOpt] = useState<string | null>(null);
  const [decision, setDecision] = useState<"" | "approve" | "modify" | "reject" | "escalate">("");
  const [reason, setReason] = useState("");
  const [reanalyze, setReanalyze] = useState(true);
  const [modVal, setModVal] = useState("");
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [gate, setGate] = useState<GateResult | null>(null);
  /** 확정 직후 표시할 한 줄 — **누른 시점**에 굳힌다 (성공 시 카드가 사라져 파생 불가). */
  const [doneNote, setDoneNote] = useState<string>("");
  const pick = (id: string) => {
    setSelId(id); setOpt(null); setDecision(""); setDone(false); setReason(""); setGate(null); setDoneNote("");
  };


  // 뒤 칸들을 선택 행 높이에 맞춰 내리는 동적 정렬은 **넣었다 뺐다** (2026-08-07).
  // 01 목록이 330px 로 잘려 자체 스크롤을 하므로 선택 행과 판정 카드의 거리가 애초에
  // 그 안이다 — 카드를 움직여 얻는 건 적고, 아래쪽 건을 고르면 위에 빈 띠만 생겼다.
  // 목록에 상한을 둔 것이 이미 같은 문제를 풀고 있었다.
  // `isSeed`/"hold" 는 dev 승계 (#124 계열) — 이 브랜치의 구 "revise" 는 그 이전 값이다
  const isDispo = String((inc as any)?.row?.request_type ?? "") === "disposition";
  /** 처분 카드에 실려온 **웨이퍼별 선택** (2026-08-11) — S6 가 만들고 게이트웨이가 카드에 담았다.
   *  구 구조는 여기에 토글 1개를 두고 전건에 같은 값을 박았다: 장별 혼재 불가 + 정비 승인과
   *  한 클릭에 묶임. 이제 이 목록이 판정 정본이고, 승인은 "이 목록대로 확정"만 한다. */
  const dispoWafers = useMemo<Array<{ wafer: string; rec: string; reason: string }>>(() => {
    const pw = (inc as any)?.ov?.wafer_disposition?.per_wafer ?? [];
    return (Array.isArray(pw) ? pw : []).map((it: any) => ({
      wafer: String(it?.wafer_id ?? ""), rec: String(it?.recommendation ?? ""),
      reason: String(it?.reason ?? ""),
    }));
  }, [inc]);
  /** 승인 화면에서 고친 웨이퍼별 판정 (2026-08-11) — 카드의 프리필 위에 덮어쓴다.
   *  결정은 여기서 한다: 02 근거를 보면서 장별로 RELEASE/SCRAP 을 확정한다. */
  const [dispoPick, setDispoPick] = useState<Record<string, "RELEASE" | "SCRAP" | "HOLD">>({});
  useEffect(() => { setDispoPick({}); }, [selId]);   // 카드 전환 시 리셋
  /** 이 장의 확정값. ""(빈값) = 미정(권고도 없고 고르지도 않음) · "HOLD" = **보류 선택**.
   *  둘은 다르다: 미정은 승인을 막고, 보류는 "이번엔 확정하지 않는다"는 명시적 결정이다. */
  const dispoFinal = (w: { wafer: string; rec: string }): "" | "RELEASE" | "SCRAP" | "HOLD" =>
    dispoPick[w.wafer] ?? (w.rec === "SCRAP" ? "SCRAP" : w.rec === "RELEASE" ? "RELEASE" : "");
  const dispoCounts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const w of dispoWafers) {
      const f = dispoFinal(w);
      if (f) c[f] = (c[f] ?? 0) + 1;
    }
    return c;
  }, [dispoWafers, dispoPick]);
  /** 판정 미정 장이 남아 있으면 승인을 막는다 — 조용한 기본값 금지. */
  const dispoUnset = useMemo(
    () => dispoWafers.filter((w) => !dispoFinal(w)).map((w) => w.wafer), [dispoWafers, dispoPick]);
  /** 확정 대상(=보류 아닌 장) 수. 0이면 승인할 게 없다 → 요청 취소를 써야 한다. */
  const dispoFinalizeN = useMemo(
    () => dispoWafers.filter((w) => { const f = dispoFinal(w); return f === "RELEASE" || f === "SCRAP"; }).length,
    [dispoWafers, dispoPick]);
  const dispoLlm = (inc as any)?.ov?.llm ?? null;
  const chosen = opt ?? (isSeed ? "seed" : isProposal ? "hold" : brief?.recommended ?? null);
  const canConfirm =
    (decision === "approve" || decision === "escalate" ||
     (decision === "modify" && modVal.trim() !== "") ||
     (decision === "reject" && reason.trim() !== ""))
    // 처분 카드 승인은 **전 장 판정 확정**을 요구한다 (2026-08-11) — 미정 장이 있으면
    // 승인이 그 장을 조용히 건너뛰거나 HOLD 로 박는다. 둘 다 감사에서 사고다.
    // 보류(HOLD)는 명시적 결정이라 허용하되, **전 장 보류면 확정할 게 없으니** 막는다
    // (그 경우는 승인이 아니라 요청 취소가 맞다 — 서버도 같은 규칙으로 거부한다).
    && !(isDispo && decision === "approve" && (dispoUnset.length > 0 || dispoFinalizeN === 0));
  const guard = decision === "modify" ? modifyGuard(modVal) : null;
  /** 이 카드가 게이트웨이에서 온 실카드인가 (목업 카드는 게이트 실패가 정상 동작이다). */
  const liveCard = !!inc?.live;
  /** 확정 한 줄 문구를 **누른 시점 상태**로 만든다 — 처분/시드/제안/일반이 서로 다른 쓰기를 한다. */
  const doneNoteFor = (act: string): string => {
    if (isSeed)
      return act === "approve" || act === "modify"
        ? "approval_records 기록(APR-PRS) → bias 파일 원자 쓰기 (SEED · mode=off) — active 는 P2 게이트 뒤"
        : "approval_records 기록 · 시드 미쓰기 (가한계 폭만 유지)";
    if (isProposal)
      return act === "approve" || act === "modify"
        ? "개정 요건 미충족 — 자 일치 원장 3건 축적 후 재평가 (마커 v1 유지 · 기록만 남음)"
        : "approval_records 기록 · 마커 v1 유지";
    // CT²(AE) 배포 (#164 병합 이식) — promote 신호 드롭 경로. correction 미발행.
    if (ct2Row)
      return act === "approve"
        ? "approval_records 기록 · control/ct2/promote_*.json 신호 드롭 → consumer 관리형 리로드 (계약 §8-E) · fdc.correction 미발행"
        : "approval_records 기록 · 번들 미배포 (challenger 보존) · correction 미발행";
    // 처분 카드는 correction 을 발행하지 않는다 — wafer_dispositions 확정 + WaferDispositioned.
    if (isDispo) {
      if (act !== "approve")
        return "approval_records 기록(REJECTED) · 원장 무변화 — 잠정 행 유지, 재요청 가능";
      const held = dispoWafers.length - dispoFinalizeN;
      return `approval_records 기록 · wafer_dispositions 확정 ${dispoFinalizeN}장`
        + (held > 0 ? ` · 보류 ${held}장 잠정 유지(decided_by 미기입)` : "")
        + " · correction 미발행";
    }
    return "approval_records 기록 · " + (act === "approve" || act === "modify"
      ? `fdc.correction 발행 (${chosen ?? "—"})` : "correction 미발행");
  };

  // P5-2 실배선 — 확정 = 게이트 resume 실호출. 실카드는 실패하면 확정 배너를 띄우지 않는다
  // (목업 카드만 게이트 없이 화면 흐름을 유지한다).
  const confirm = async () => {
    if (!canConfirm || busy || done) return;
    setBusy(true);
    const payload: DecisionPayload = { action: decision as DecisionPayload["action"], ...APPROVER };
    // 🔴 카드 지목 (2026-08-11) — 정비/처분 PENDING 이 공존하므로 **결정한 카드의 record_id**
    //   를 보낸다. 없으면 서버가 "가장 최근 PENDING" 으로 라우팅해 정비 승인이 처분을 확정한다.
    const recId = Number((inc as any)?.row?.record_id ?? NaN);
    if (Number.isFinite(recId)) payload.record_id = recId;
    if (decision === "modify") payload.modified_value = modVal.trim();
    if (decision === "reject") { payload.reason = reason.trim(); payload.reanalyze = reanalyze; }
    // 처분 카드 — **웨이퍼별 확정값**을 함께 보낸다 (2026-08-11). 서버가 카드의 프리필 목록
    // 위에 덮어쓰고, 바뀐 장은 사유에 "승인 시 엔지니어 확정(X) — 요청 시점 Y" 로 남는다.
    if (decision === "approve" && isDispo && dispoWafers.length) {
      payload.per_wafer = dispoWafers.map((w) => ({
        // 'HOLD' 도 그대로 보낸다 — 서버가 확정 대상에서 빼고 잠정 행을 남긴다 (부분 확정).
        // 미정("")은 canConfirm 이 막지만, 뚫려도 서버가 400 으로 거부한다(조용한 기본값 금지).
        wafer_id: w.wafer, disposition: dispoFinal(w) as "RELEASE" | "SCRAP" | "HOLD",
      }));
    }
    const g = await postDecision(apiId, payload);  // 〃 (그래프 resume 는 Incident 기준)
    setGate(g);
    // 라이브 카드에서 게이트가 거절하면 **확정된 게 아니다** (2026-08-11).
    //   구 코드는 무조건 done=true → 게이트 400 을 받고도 "✓ APPROVED" 를 띄웠다. 원장은
    //   그대로인데 화면만 승인이라, 촬영에서 실패를 성공으로 읽게 만든다. 실패면 확정 배너를
    //   띄우지 않고 버튼도 살려둬서 고쳐서 다시 누를 수 있게 한다.
    //   목업 카드(CASE-0912/SEED/PROPOSAL)는 게이트가 없는 게 정상이라 종전대로 진행한다.
    if (g.live || !liveCard) { setDoneNote(doneNoteFor(decision)); setDone(true); }
    setBusy(false);
    if (g.live) refreshPending();   // 실승인 성공 → PENDING 해소분 즉시 반영 (카드 소멸)
  };

  return (
    <div className="scr">
      <div className="scrh">
        <div>
          <div className="t">Approvals</div>
          <div className="c num">LANGGRAPH RESUME · INCIDENT당 동시 PENDING 1건 (헌법 1-4)</div>
        </div>
      </div>

      <div className="chain">
        {/* ① 인시던트 */}
        <div className="cnode">
          <div className="cnh num">01 · INCIDENT</div>
          {open.map((i) => (
            <button key={i.id} className={"cinc" + (i.id === selId ? " on" : "")} onClick={() => pick(i.id)}>
              <span className={"qtick " + (i.sev === "critical" ? "crit" : "warn")} />
              <span className="tt">{i.title}</span>
              <span className="ss num">{i.chamber} · score {i.score}
                {/* 2026-08-03 §3-1: else 가지가 비어 있어 목업 카드가 무표식이었다.
                    PENDING 0건이면 첫 화면이 CASE-0912 목업으로 채워지는데 청중은 구분할 수 없다. */}
                {(i as LiveInc).live ? " · ● LIVE" : " · ○ MOCK"}{i.regime ? " · REGIME" : ""}
                {/* 누적 표류 배지 (#124, dev) — 두 표식은 서로 독립이라 둘 다 남긴다. */}
                {(i as LiveInc).cum?.warned ? ` · ⚠ 누적 ${(i as LiveInc).cum!.sigma}σ` : ""}</span>
            </button>
          ))}
          {/* SYSTEM 제안 — 자동 경로 없음, 승인 경유 (원칙 4호). 값 = 8/9 원장·게이트웨이 실측 */}
          <div className="cnh num" style={{ marginTop: 10 }}>SYSTEM · 자기 개선 제안</div>
          <button className={"cinc" + (isSeed ? " on" : "")} onClick={() => pick(S.id)}>
            <span className="qtick" style={{ background: "var(--mint)" }} />
            <span className="tt">{S.title}</span>
            <span className="ss num">{S.transitionId} · 원장 {S.ledger.n}건 · '추정' · SEED</span>
          </button>
          <button className={"cinc" + (isProposal ? " on" : "")} onClick={() => pick(P.id)}>
            <span className="qtick" style={{ background: "var(--faint)" }} />
            <span className="tt">{P.title}</span>
            <span className="ss num">원장 {P.ledgerN}건 · 자 일치 {P.ledgerMatched}/3 · 보류</span>
          </button>
        </div>
        <div className="carrow">→</div>

        {/* ② 상황·근거 */}
        <div className="cnode wide">
          <div className="cnh num">02 · 판정 & 근거</div>

          {/* A-warn 누적 표류 (2026-08-07) — Brief 유무와 무관하게 항상 먼저 보인다.
              엔지니어가 승인 버튼을 누르기 전에 봐야 하는 값이라 판정문 위에 둔다. */}
          {(inc as LiveInc | undefined)?.cum?.warned && (
            <div className="ev counter" style={{ borderColor: "var(--alarm)" }}>
              <i className="num">누적</i>
              최초 기준선(v1) 대비 <b>{(inc as LiveInc).cum!.sigma}σ</b> 이동
              {(inc as LiveInc).cum!.sensor ? ` · ${(inc as LiveInc).cum!.sensor}` : ""}
              {(inc as LiveInc).cum!.thr != null ? ` (경고 임계 ${(inc as LiveInc).cum!.thr}σ)` : ""}
              {" — 개별 승인은 매번 상한 안이었어도 누적은 이만큼입니다."}
            </div>
          )}
          {ct2Row ? (
            /* CT²(AE) 배포 — SPC 4지선다가 아니라 게이트 브리프를 보여준다 (개정 v2 §7).
               결정은 04 에서 승인/반려 2지선다로 좁혀진다 (계약 §8-E). */
            <Ct2Brief row={ct2Row} />
          ) : isSeed ? (
            <>
              <div className="verdict">
                <span className="vp" style={{ background: "var(--mint)" }} />
                <b>프라이어 시드 — 요란 전이 D+0 기준선 (R9 신규 수립)</b>
                <span className="num" style={{ color: "var(--faint)" }}>원장 {S.ledger.n}건 · 주입 {S.ledger.injected}</span>
              </div>
              <div className="vreason">
                Δ {S.delta} ({S.deltaBasis}) × {S.marker} → 시드 <b>+{S.seedBias}</b> — 점근 상수 · '추정'.
                라벨 D+60 도착 전 2달의 다리 — rolling(r2r)이 인수하면 소멸.
              </div>
              <div className="ev"><i className="num">E1</i>
                bias_{S.chamberRef}.json — #152 계약 동형 · status=SEED · ct_id=승인 기록 · <b>mode=off</b> (active 는 P2 Scorecard 게이트 뒤)
              </div>
              <div className="ev"><i className="num">E2</i>
                감쇠 템플릿 τ {S.template.tauDays}일 · A {S.template.amplitude} — {S.template.note}
              </div>
              <div className="ev"><i className="num">E3</i>
                직전 유사 전이 실측 점근 +{S.refActual} 대조 — 시드와의 간극이 자 불일치(scenario_def × 실자 마커)의 크기다 (숨기지 않고 노출)
              </div>
              <div className="ev counter"><i className="num">반증</i>{S.basisNote} — {S.guard}</div>
            </>
          ) : isProposal ? (
            <>
              <div className="verdict">
                <span className="vp" style={{ background: "var(--mint)" }} />
                <b>프라이어 자기 개선 — 마커 개정 트랙</b>
                <span className="num" style={{ color: "var(--faint)" }}>원장 {P.ledgerN}건 · 자 일치 {P.ledgerMatched}</span>
              </div>
              <div className="vreason">{P.current} → {P.proposed}</div>
              {P.candidates.map((c, i) => (
                <div className="ev" key={i}>
                  <i className="num">E{i + 1}</i>
                  {c.marker} — 프라이어 오차 <b>{c.errPct}%</b>{c.note ? ` (${c.note})` : ""}
                </div>
              ))}
              {P.errTrend.length > 0 && (
                <div style={{ padding: "6px 2px 2px" }}>
                  <div className="num" style={{ fontSize: 9.5, color: "var(--faint)", marginBottom: 8 }}>
                    프라이어 오차 추이 (%) — 전이 회차별 · 수렴 곡선
                  </div>
                  {/* 셀 = [바(위로 성장) + 라벨] 세로 스택, 고정 높이 제거 — 겹침 방지 (2026-07-20) */}
                  <div style={{ display: "flex", gap: 5, alignItems: "flex-end" }}>
                    {P.errTrend.map((v, i) => (
                      <span key={i} className="num" style={{ display: "grid", justifyItems: "center", gap: 3 }}>
                        <span style={{ width: 14, height: Math.max(3, Math.round(v * 0.7)), background: "var(--mint)", opacity: .8, borderRadius: 2 }} />
                        <span style={{ fontSize: 8, color: "var(--faint)" }}>{v}</span>
                      </span>
                    ))}
                  </div>
                </div>
              )}
              <div className="ev counter"><i className="num">반증</i>{P.counter}</div>
            </>
          ) : brief ? (
            <>
              <div className="verdict">
                <span className="vp" style={{ background: VERDICT_COLOR[brief.verdict] }} />
                <b>{brief.verdictKo}</b>
                <span className="num" style={{ color: "var(--faint)" }}>conf {brief.confidence.toFixed(2)}</span>
              </div>
              <div className="vreason">{brief.reason}</div>
              {brief.evidence.map((e, i) => (
                <div className="ev" key={i}><i className="num">E{i + 1}</i>{e}</div>
              ))}
              <div className="ev counter"><i className="num">반증</i>{brief.counter}</div>
              {/* Brief 출처 표기 (2026-08-11 · 처분 카드) — LLM 이 썼는지 fallback 인지 숨기지
                  않는다. conf 0.00 fallback 을 LLM 판단으로 읽히게 두면 실값 위장이다. */}
              {isDispo && dispoLlm && (
                <div className="num" style={{ fontSize: 9.5, color: "var(--faint)", padding: "4px 2px 0" }}>
                  {dispoLlm.ok
                    ? `Brief 생성: ${String(dispoLlm.model ?? "LLM")} · ${dispoLlm.latency_ms ?? "—"}ms`
                    : `Brief fallback — LLM 실패 (${String(dispoLlm.why ?? "미상")}) · 근거는 원장·예측 실값 인용`}
                </div>
              )}
            </>
          ) : (
            <div className="vreason" style={{ color: "var(--faint)" }}>
              리포트 수합 중 (ANALYZING) — Supervisor Brief 대기.{inc && inc.score < 31 ? " score<31 — 수동 분석 요청 건." : ""}
            </div>
          )}

          {/* 증거층 — 판단(위)은 생성 시점에 얼렸고, 아래는 **지금** 값이다.
              낡았으면 감추지 않고 배지로 드러내고, 갱신은 사람이 [재분석] 으로 당긴다. */}
          {evid && (
            <div className="evl">
              <div className="evh">
                <span>증거 · 알람 {evid.alarms}건 · 센서 {evid.sensors.length}종</span>
                {evid.brief_version != null && <span>· Brief v{evid.brief_version}</span>}
                {evid.since_brief.stale && (
                  <span className="stale">
                    생성 이후 +{evid.since_brief.alarms}건
                    {evid.since_brief.new_sensors.length > 0 &&
                      ` · 신규 센서 ${evid.since_brief.new_sensors.join(", ")}`}
                  </span>
                )}
                {evid.needs_reanalysis
                  ? <span className="rebtn" style={{ cursor: "default" }}>재분석 요청됨</span>
                  : <button className="rebtn" onClick={pullReanalysis} disabled={reBusy}>
                      {reBusy ? "요청 중…" : "재분석"}
                    </button>}
              </div>
              {evid.violations.slice(0, 6).map((v, i) => (
                <div key={`${v.alert_id}-${i}`}
                     className={"evrow" + (String(v.severity).toUpperCase() === "CRITICAL" ? " crit" : "")
                                + (v.is_new ? " fresh" : "")}>
                  <span className="sid">{v.sensor ?? "—"}</span>
                  <span className="rid num">{v.rule_id ?? "—"}</span>
                  <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {/* description 이 "N1: ..." 로 시작하면 접두를 뗀다 — 왼쪽 rid 배지와 중복 */}
                    {(v.description ?? "—").replace(new RegExp(`^${v.rule_id ?? ""}:\\s*`), "")}
                  </span>
                  {v.current_value != null &&
                    <span className="num" style={{ color: "var(--faint)" }}>{Number(v.current_value).toFixed(1)}</span>}
                </div>
              ))}
              {(evid.violations.length > 6 || evid.violations_truncated) && (
                <div className="evrow" style={{ color: "var(--faint)" }}>
                  … 외 {Math.max(0, evid.violations.length - 6)}건
                  {evid.violations_truncated && " +"} (S3 에서 전체 확인)
                </div>
              )}
            </div>
          )}
        </div>
        <div className="carrow">→</div>

        {/* ③ 옵션 3종 */}
        <div className="cnode wide">
          <div className="cnh num">03 · {isDispo ? `처분 목록 (웨이퍼 ${dispoWafers.length}장 · 엔지니어 선택)`
            : isSeed ? "옵션 (시드 결정)" : isProposal ? "옵션 (마커 결정)" : "옵션 (병렬 리포트 3종)"}</div>
          {/* 처분 카드 — 4지선다가 아니라 **웨이퍼별 판정 목록**이 온다 (2026-08-11).
              이 배열이 확정 SQL 의 입력과 동일하다: 화면이 본 것과 원장에 박히는 것이 같은 값. */}
          {isDispo ? (
            dispoWafers.length ? (
              <>
                {/* 일괄 지정 — 5장 전부 같은 판정인 경우가 흔하다(권고 동의). 한 번에 깔고
                    예외 장만 개별로 뒤집는 게 실제 작업 순서다 (2026-08-11 실측 피드백). */}
                {/* 「전체 RELEASE/SCRAP」 일괄 버튼 제거 (2026-08-11 PM) — 전건 지정은 S6 의
                    [RELEASE로 요청]/[SCRAP으로 요청] 이 이미 담당한다. 카드는 **장별 판단**의
                    자리이고 03 열은 좁아서, 중복 버튼을 빼면 그만큼 웨이퍼가 더 보인다.
                    (5장 이상을 카드에서 한 번에 뒤집을 일이 생기면 여기 되살리면 된다.) */}
                <div className="num" style={{ fontSize: 9.5, marginBottom: 8,
                     color: dispoUnset.length ? "var(--alarm)" : "var(--faint)" }}>
                  {dispoUnset.length
                    ? `판정 미정 ${dispoUnset.length}장 — 전 장 고른 뒤 승인됩니다.`
                    : dispoFinalizeN === 0
                    ? "전 장 보류 — 확정할 웨이퍼가 없습니다. 승인 대신 [요청 취소]를 쓰세요."
                    : `확정 ${dispoFinalizeN}장${dispoCounts.HOLD ? ` · 보류 ${dispoCounts.HOLD}장` : ""} — 보류 장은 이번 승인에서 빠지고 잠정 유지됩니다.`}
                </div>
                {dispoWafers.map((w) => {
                  const f = dispoFinal(w);
                  const changed = !!dispoPick[w.wafer] && dispoPick[w.wafer] !== w.rec;
                  return (
                  <div key={w.wafer} className="opt" style={{ cursor: "default" }}>
                    {/* ⚠️ 03 열은 좁다 — 웨이퍼ID·토글·배지를 한 줄에 몰면 칩이 세로로 접혀
                        "✓ / SCRAP" 처럼 쪼개진다(8/11 실측). 3단 세로 스택으로 고정한다. */}
                    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                      <b className="num" style={{ whiteSpace: "nowrap" }}>{w.wafer}</b>
                      {changed && <span className="rec num" style={{ whiteSpace: "nowrap" }}>변경</span>}
                      {f === "SCRAP" && (
                        <span className="rec num" style={{ whiteSpace: "nowrap", background: "var(--pink-soft)", color: "var(--pink-deep)" }}>비가역</span>
                      )}
                      {!f && (
                        <span className="rec num" style={{ whiteSpace: "nowrap", background: "var(--amber-soft, #fef3e0)", color: "var(--amber-deep, #b8720f)" }}>미정</span>
                      )}
                    </div>
                    {/* 판정 선택 — **여기가 결정 지점**이다. 선택된 쪽은 강조색으로 꽉 채우고
                        (✓ 표시), 안 고른 쪽은 읽히는 밝기로 남긴다: 종전엔 soft 배경 + faint
                        글자라 "누를 수 있는 것"으로 안 보였다(8/11 피드백 2회). */}
                    {/* 각 칩에 **자기 테두리**를 준다 — 종전엔 바깥 span 만 var(--line) 테두리라
                        어두운 배경에서 사실상 안 보였고, 안 고른 쪽은 그냥 글자처럼 읽혔다
                        (8/11 PM: "테두리가 없으니 선택해야 하는 건지 모르겠다"). 두 칩이 폭을
                        반반 차지해 히트 영역도 넓힌다. */}
                    {/* 3지선다 — RELEASE / SCRAP / 보류. 보류는 **이번 승인에서 이 장을 빼는**
                        선택이다(잠정 행 그대로 유지 · decided_by 미기입): 카드 5장 중 3장만
                        처리하고 2장은 다음에 판단하는 실제 작업을 그대로 표현한다(PM 8/11). */}
                    <div style={{ display: "flex", gap: 5, marginTop: 7 }}>
                      {([["RELEASE", "var(--mint)", "정상 투입"],
                         ["SCRAP", "var(--pink)", "폐기 — 비가역"],
                         ["HOLD", "var(--amber, #d9a13b)", "보류 — 이번 승인에서 제외, 잠정 유지"]] as const)
                        .map(([v, accent, tip]) => {
                        const on = f === v;
                        return (
                          <button key={v}
                                  onClick={() => { setDispoPick((p) => ({ ...p, [w.wafer]: v })); setDone(false); }}
                                  title={tip}
                                  style={{ flex: 1, cursor: "pointer", fontSize: 10, letterSpacing: .2,
                                           padding: "6px 0", borderRadius: 6, whiteSpace: "nowrap",
                                           lineHeight: 1.2, fontWeight: on ? 700 : 600,
                                           border: `1px solid ${accent}`,
                                           background: on ? accent : "transparent",
                                           color: on ? "#0b1220" : accent }}>
                            {on ? "✓ " : ""}{v === "HOLD" ? "보류" : v}
                          </button>
                        );
                      })}
                    </div>
                    <div className="oa" style={{ marginTop: 7 }}>{w.reason || "—"}</div>
                  </div>
                  );
                })}
              </>
            ) : (
              <div className="vreason" style={{ color: "var(--alarm)" }}>
                처분 목록이 비어 있습니다 — 승인해도 확정 대상이 없습니다 (S6 에서 다시 요청).
              </div>
            )
          ) : isSeed ? ([
            { k: "seed", t: "시드 쓰기 — bias 파일 (mode=off)", d: "approval_records 기록 → bias_" + S.chamberRef + ".json 원자 쓰기 · active 는 P2 게이트 실증 뒤", rec: true },
            { k: "keep", t: "미적용 — 가한계 폭만 사용", d: "시드 없이 표시·가한계 유지 — D+60 rolling 인수 대기 (2달 무보정)", rec: false },
            { k: "hold", t: "보류 — 전이 재검증", d: "Qual 판독 재확인 후 재제안 (원장 행은 유지)", rec: false },
          ] as const).map((o) => (
            <button key={o.k} className={"opt" + (chosen === o.k ? " on" : "")} onClick={() => setOpt(o.k)}>
              <div className="oh">
                <span className="atype lim">PRI</span>
                <b>{o.t}</b>
                {o.rec && <span className="rec num">추천 1위</span>}
              </div>
              <div className="oa">{o.d}</div>
            </button>
          )) : isProposal ? ([
            { k: "hold", t: "보류 — 자 일치 원장 축적", d: "자 일치 " + P.ledgerMatched + "/3건 — 개정 불가 · scenario_def 5건은 메커니즘 검증 전용 (v1 재캘리 사용 금지)", rec: true },
            { k: "keep", t: "유지 — v1 (C17 단독)", d: "실측 전이 1건 앵커 · Qual 읽기 자(qual5-seg1→last1000) 캘리브레이션", rec: false },
          ] as const).map((o) => (
            <button key={o.k} className={"opt" + (chosen === o.k ? " on" : "")} onClick={() => setOpt(o.k)}>
              <div className="oh">
                <span className="atype lim">PRI</span>
                <b>{o.t}</b>
                {o.rec && <span className="rec num">추천 1위</span>}
              </div>
              <div className="oa">{o.d}</div>
            </button>
          )) : brief ? brief.options.map((o) => (
            <button
              key={o.key}
              className={"opt" + (chosen === o.key ? " on" : "") + (o.rejected ? " rej" : "")}
              onClick={() => setOpt(o.key)}
            >
              <div className="oh">
                <span className={"atype " + (o.key === "limit" ? "lim" : o.key === "recipe" ? "rcp" : "mnt")}>
                  {o.key === "limit" ? "LIM" : o.key === "recipe" ? "RCP" : "MNT"}
                </span>
                <b>{o.title}</b>
                {brief.recommended === o.key && <span className="rec num">추천 1위</span>}
                <span className="num" style={{ marginLeft: "auto", color: "var(--faint)" }}>{o.confidence.toFixed(2)}</span>
              </div>
              <div className="oa">{o.action} · {o.feasibility}</div>
              {o.rejected ? <div className="orj">기각 사유 — {o.rejected}</div>
                : o.note ? <div className="orj">유보 — {o.note}</div> : null}
            </button>
          )) : <div className="vreason" style={{ color: "var(--faint)" }}>—</div>}
        </div>
        <div className="carrow">→</div>

        {/* ④ 결정 */}
        <div className="cnode">
          <div className="cnh num">04 · 결정</div>
          {/* 처분 카드 (2026-08-11) — 판정 토글은 여기 없다. 판정은 S6 에서 웨이퍼별로 이미
              골랐고(03 패널이 그 목록), 이 승인은 "그 목록대로 확정"이다. 종전처럼 여기 토글을
              두면 정비 승인과 한 클릭에 묶이고 장별 혼재가 불가능해진다. */}
          {isDispo && (
            <div className="num" style={{ fontSize: 9.5, marginBottom: 6,
                 color: dispoUnset.length ? "var(--alarm)" : "var(--faint)" }}>
              {dispoUnset.length
                ? `03 에서 판정 미정 ${dispoUnset.length}장 — 전 장 고른 뒤 승인 가능`
                : dispoFinalizeN === 0
                ? "전 장 보류 — 확정 대상 0장 (요청 취소를 쓰세요)"
                : `승인 = 03 확정값대로 기록 (${Object.entries(dispoCounts).filter(([k]) => k !== "HOLD").map(([k, v]) => `${k} ${v}장`).join(" · ")})`}
              {!dispoUnset.length && dispoFinalizeN > 0 && dispoCounts.HOLD ? ` · 보류 ${dispoCounts.HOLD}장 유지` : ""}
              {!dispoUnset.length && dispoFinalizeN > 0 && dispoCounts.SCRAP ? " · SCRAP 비가역" : ""}
            </div>
          )}
          {/* 처분 카드는 **2지선다**다 (2026-08-11 PM) — 4지선다는 여기서 다 무의미하다:
              ·수정 승인 = limit 수정값 기입 경로(처분엔 수정할 스칼라가 없다. 판정 변경은 03에서)
              ·에스컬레이션 = Incident 재개 신호(처분은 lifecycle 을 안 건드린다)
              반려는 남긴다 — 잘못 연 카드를 닫는 유일한 길이고, 없으면 그 Incident 의 처분
              요청이 영구히 열려 있어 새 요청이 409 로 막힌다. 라벨을 실제 효과로 적는다. */}
          {/* ★CT²(AE) 배포도 **2지선다**다 (#164) — 번들은 수정 대상이 아니므로 서버가 modify·escalate 를
              400 으로 거부한다 (계약 §8-E · main.py). 버튼을 남겨두면 400 이 목업 폴백으로 흡수돼
              화면엔 큼직하게 `✓ APPROVED (MODIFIED)` 가 뜨는데 approval_records 엔 아무 것도
              없다 — "예외가 안 났으니 성공"(TSR-0002)과 같은 형태의 유령 승인이다. */}
          {(isDispo || ct2Row ? (["approve", "reject"] as const)
                              : (["approve", "modify", "reject", "escalate"] as const)).map((d) => (
            <button key={d} className={"dbtn " + d + (decision === d ? " on" : "")} onClick={() => { setDecision(d); setDone(false); }}>
              {d === "approve" ? "승인"
                : d === "modify" ? "수정 승인"
                : d === "reject" ? (isDispo ? "요청 취소" : "반려")
                : "에스컬레이션"}
            </button>
          ))}
          <div className="cnfoot num">
            {isDispo ? "취소 = 처분 요청만 닫음 · 웨이퍼는 HOLD 유지 (원장 무변화)"
              : ct2Row ? "CT² 배포 = 승인/반려 2지선다 (계약 §8-E)"
              : "reject = 엔지니어 주도 (v1.4)"}
          </div>
        </div>

        {/* ⑤ 조건 노드 */}
        {(decision === "reject" || decision === "modify") && (
          <>
            <div className="carrow">→</div>
            <div className="cnode">
              <div className="cnh num">05 · {decision === "reject" ? (isDispo ? "취소 사유" : "반려 상세") : "수정 값"}</div>
              {decision === "reject" ? (
                <>
                  <textarea className="cinput"
                            placeholder={isDispo ? "취소 사유 (필수) — 예: 대상 웨이퍼 재선정 필요"
                                                 : "반려 사유 (필수)"}
                            value={reason} onChange={(e) => setReason(e.target.value)} />
                  {/* 재분석/종료 = Incident lifecycle 거취. 처분 취소는 lifecycle 을 건드리지
                      않으므로(원장·본선 무영향) 이 토글이 의미 없다 — 처분 카드에서는 숨긴다. */}
                  {!isDispo && (
                    <div className="tgl">
                      <button className={"fchip" + (reanalyze ? " on" : "")} onClick={() => setReanalyze(true)}>
                        <span>재분석</span><span className="sub num">(analyzing)</span>
                      </button>
                      <button className={"fchip" + (!reanalyze ? " on" : "")} onClick={() => setReanalyze(false)}>
                        <span>종료</span><span className="sub num">(closed)</span>
                      </button>
                    </div>
                  )}
                </>
              ) : (
                <>
                  <input className="cinput" placeholder="수정 값 (예: +3.2% / +0.4σ)" value={modVal} onChange={(e) => setModVal(e.target.value)} />
                  {guard && (
                    <div className="num" style={{ color: "var(--alarm)", fontSize: 10, marginTop: 4 }}>⚠ {guard}</div>
                  )}
                </>
              )}
            </div>
          </>
        )}

        <div className="carrow">→</div>
        {/* ⑥ 확정 */}
        <div className="cnode">
          <div className="cnh num">확정</div>
          <button className="gopill" disabled={!canConfirm || done || busy} style={{ opacity: canConfirm && !done && !busy ? 1 : .45 }} onClick={confirm}>
            {busy ? "게이트 호출 중…" : "Command(resume) 실행"}
          </button>
          {done && (
            <div className="cdone">
              <div className="num" style={{ color: "var(--mint-deep)", fontWeight: 700 }}>✓ {decision === "approve" ? "APPROVED" : decision === "modify" ? "APPROVED (MODIFIED)" : decision === "reject" ? (reanalyze ? "REJECTED → 재분석" : "REJECTED → 종료") : "ESCALATED"}</div>
              {/* 누른 시점에 굳힌 한 줄 (doneNoteFor) — 성공하면 카드가 목록에서 사라져
                  inc 파생값이 전부 무너진다. 종전엔 그래서 처분 승인에도 엉뚱하게
                  "fdc.correction 발행 (—)" 이 찍혔다. CT² 문구(#164)는 doneNoteFor 안에 있다. */}
              <div className="num" style={{ color: "var(--faint)", fontSize: 10 }}>{doneNote}</div>
              {gate && (
                <div className="num" style={{ fontSize: 10, marginTop: 3, color: gate.live ? "var(--mint-deep)" : "var(--alarm)" }}>
                  {gate.live ? "● LIVE — " : "○ MOCK — "}{gate.detail}
                </div>
              )}
            </div>
          )}
          {/* 게이트 거절 (라이브 카드) — 확정이 아니다. done 을 세우지 않으므로 여기로 온다. */}
          {gate && !gate.live && !done && (
            <div className="cdone">
              <div className="num" style={{ color: "var(--alarm)", fontWeight: 700 }}>✕ 미확정 — 게이트 거절</div>
              <div className="num" style={{ color: "var(--faint)", fontSize: 10 }}>
                원장 무변화 · PENDING 유지 — 사유를 고친 뒤 [Command(resume) 실행] 다시
              </div>
              <div className="num" style={{ fontSize: 10, marginTop: 3, color: "var(--alarm)" }}>{gate.detail}</div>
            </div>
          )}
          <div className="cnfoot num">Incident당 pending 1건 · 감사 추적 보존</div>
        </div>
      </div>
    </div>
  );
}
