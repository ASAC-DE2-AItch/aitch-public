// P5-2 실배선 — Gateway 승인 API (vite 프록시: /api/* → :8000/*)
// 원칙: 실패해도 화면은 죽지 않는다 — 목업 폴백 + 상태 표기 (시연 안전망, P5-4와 동일 패턴)
// 계약: POST /approvals/{incident_id} — validate_decision (src/gateway/approvals.py 단일 소스)
//   필수 action·approver / modify→modified_value / reject→reason(+reanalyze)

export type GateAction = "approve" | "modify" | "reject" | "escalate";

/** 처분 카드 승인 시 함께 보내는 **웨이퍼별 확정값** (2026-08-11) — 카드에 프리필된 권고를
 *  엔지니어가 근거 보면서 고친 결과. 서버가 카드의 목록 위에 덮어써 확정한다. */
export interface PerWaferDecision {
  wafer_id: string;
  /** HOLD = **이 장은 확정하지 않고 잠정 유지** (부분 확정 · 2026-08-11).
   *  카드 5장 중 3장만 처리하고 2장은 다음에 판단하는 경우가 이 값이다. */
  disposition: "RELEASE" | "SCRAP" | "HOLD";
}

export interface DecisionPayload {
  action: GateAction;
  approver: string;
  approver_role?: string;
  reason?: string;
  modified_value?: string;
  reanalyze?: boolean;
  /** 처분 선택 (2026-08-10) — 전건 동일 판정. **구 경로**: 장별 혼재가 불가능해
   *  per_wafer 로 대체됐다. 서버는 아직 받지만 화면은 쓰지 않는다. */
  disposition?: "RELEASE" | "SCRAP";
  /** 웨이퍼별 확정값 (2026-08-11) — 처분 카드 approve 전용. 정본 경로. */
  per_wafer?: PerWaferDecision[];
  /** 🔴 어느 카드를 결정했는가 = approval_records.id (2026-08-11). 한 Incident 에 정비·처분
   *  PENDING 이 공존하므로(헌법 1-4 예외), 이걸 안 보내면 서버가 "가장 최근 PENDING" 을
   *  집어 **정비 승인이 처분을 확정**할 수 있다. 실카드는 반드시 보낸다. */
  record_id?: number;
}

export interface GateResult {
  /** true = LangGraph resume 실호출 성공 / false = 오프라인·오류 → 목업 폴백 */
  live: boolean;
  detail: string;
}

/** 데모 페르소나 — 로그인 없음, 감사 추적(approver 필수)용 고정 계정 */
export const APPROVER = { approver: "AITCH", approver_role: "Etch PE" };

const TIMEOUT_MS = 2500;
/** 결정(resume)은 폴링과 달리 **쓰기**다 — DB 커밋 + Kafka 발행 + 그래프 재개가 한 요청에 들어간다.
 *  2.5s 로 끊으면 서버가 정상 처리 중인데 화면만 "응답 없음" 이 되고, 그때 반영 여부를 알 수 없다
 *  (2026-08-11). 처분 승인은 finalize + 이벤트까지 하므로 넉넉히 기다린다. */
const DECISION_TIMEOUT_MS = 8000;

export async function postDecision(incidentId: string, p: DecisionPayload): Promise<GateResult> {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), DECISION_TIMEOUT_MS);
  try {
    const r = await fetch(`/api/approvals/${encodeURIComponent(incidentId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(p),
      signal: ctl.signal,
    });
    const body = await r.json().catch(() => ({} as Record<string, unknown>));
    if (!r.ok) {
      // 서버가 거절 = 원장 무변화가 확실하다. "목업 폴백" 이라고 쓰면 실카드에서 승인처럼
      // 읽힌다 (2026-08-11) — 화면이 실패 배너를 띄우므로 여기선 사유만 그대로 넘긴다.
      return { live: false, detail: `게이트 ${r.status} — ${String((body as any).detail ?? "오류")}` };
    }
    return { live: true, detail: "LangGraph resume 실행 — approval_records 기록·lifecycle 전이" };
  } catch {
    // 타임아웃/네트워크 — 서버가 뒤늦게 처리했을 수 있어 **반영 여부를 단정하지 않는다**.
    return { live: false, detail: `게이트 응답 없음 (${DECISION_TIMEOUT_MS}ms 초과) — 원장 반영 여부 미확인, 목록 갱신으로 확인` };
  } finally {
    clearTimeout(t);
  }
}

/** S3 승인 큐 실배선용 (후속) — PENDING 목록. 실패 시 null → mock 유지 */
export async function fetchPending(): Promise<unknown[] | null> {
  try {
    const r = await fetch("/api/approvals/pending", { signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!r.ok) return null;
    const b = await r.json();
    return Array.isArray(b?.pending) ? b.pending : null;
  } catch {
    return null;
  }
}

// ---- S3 Incidents 실배선 (2026-07-30) — GET /incidents/recent ----------------
// 리허설 실측에서 백엔드는 관통(알람→Incident)했는데 S3 만 목업이라 아무것도 안 보였다.
export interface LiveIncident {
  incident_id: string;
  chamber_id: string;
  lifecycle: string;              // open|analyzing|pending|verifying|reopened|closed
  incident_type: string | null;   // null=일반 / crazy_spot / regime_transition
  severity_max: string | null;
  priority_score: number | null;
  reopen_count: number;
  is_escalated: boolean;
  claimed_by: string | null;
  created_at: string;
  updated_at: string;
  alarms: number;
  context_score: number;          // max — 분류용(가장 심각한 알람)
  context_score_avg: number | null;  // avg — 전반 수준
  title: string | null;           // agent_reports 최신 supervisor_reason (Agent 미가동이면 null)
  verdict: string | null;
  age_sec: number | null;
}

export async function fetchIncidents(n = 60): Promise<LiveIncident[] | null> {
  try {
    const r = await fetch(`/api/incidents/recent?n=${n}`, { signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!r.ok) return null;
    const b = await r.json();
    return Array.isArray(b?.incidents) ? b.incidents : null;
  } catch { return null; }
}

/** 증거층 (2026-07-31 — PR #70 서사층/증거층 분리). 화면이 조회 시점에 읽는 현재 데이터. */
export interface EvidenceViolation {
  alert_id: string; alert_ts: string | null;
  sensor: string | null; rule_id: string | null; severity: string | null;
  description: string | null; current_value: number | null; limit_version: string | null;
  is_new: boolean;                 // Brief 생성 이후 편입분
}
export interface IncidentEvidence {
  incident_id: string;
  alarms: number;
  violations: EvidenceViolation[];
  violations_truncated: boolean;   // 목록만 상한 — 집계(alarms)는 전체다
  sensors: string[];
  since_brief: { alarms: number; new_sensors: string[]; stale: boolean };
  brief_version: number | null;
  needs_reanalysis: boolean;
  brief_created_at: string | null;
}

export async function fetchEvidence(incidentId: string): Promise<IncidentEvidence | null> {
  try {
    const r = await fetch(`/api/incidents/${encodeURIComponent(incidentId)}/evidence`,
                          { signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }        // 게이트 오프라인 — 서사층만 표시 (실값 위장 금지)
}

/** [재분석] — 재생성을 사람이 당긴다 (자동 갱신 금지 — pending 이 몰래 바뀌면 감사가 깨진다) */
export async function requestReanalysis(incidentId: string): Promise<boolean> {
  try {
    const r = await fetch(`/api/incidents/${encodeURIComponent(incidentId)}/reanalyze`,
                          { method: "POST", signal: AbortSignal.timeout(TIMEOUT_MS) });
    return r.ok;
  } catch { return false; }
}

/** [AI 분석 요청] — 게이트(31) 미달 Incident 의 수동 첫 분석 (스텁 심기 → C 폴러 우회 생성, 2026-08-12) */
export async function requestAnalyze(incidentId: string): Promise<boolean> {
  try {
    const r = await fetch(`/api/incidents/${encodeURIComponent(incidentId)}/analyze`,
                          { method: "POST", signal: AbortSignal.timeout(TIMEOUT_MS) });
    return r.ok;
  } catch { return false; }
}

// ---------------------------------------------------------------------------
// P6-3 — S11 시뮬레이터 실배선 (RUN = CLI와 동일 효과, 실패 시 목업 연출 폴백)
// ---------------------------------------------------------------------------
export interface SimScenario {
  id: string;
  scenario_id?: string;
  pattern?: string;
  chambers?: string[];
  description?: string;
  ground_truth_type?: string;
  error?: string;
}
export interface SimStatus {
  running: boolean;
  scenario: string | null;
  started_at?: string | null;
  returncode?: number | null;
  last_line?: string | null;
}

export async function fetchScenarios(): Promise<SimScenario[] | null> {
  try {
    const r = await fetch("/api/simulator/scenarios", { signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!r.ok) return null;
    const b = await r.json();
    return Array.isArray(b?.scenarios) ? b.scenarios : null;
  } catch { return null; }
}

export async function simStatus(): Promise<SimStatus | null> {
  try {
    const r = await fetch("/api/simulator/status", { signal: AbortSignal.timeout(TIMEOUT_MS) });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

export async function simLogs(n = 60): Promise<string[] | null> {
  try {
    const r = await fetch(`/api/simulator/logs?n=${n}`, { signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!r.ok) return null;
    const b = await r.json();
    return Array.isArray(b?.lines) ? b.lines : null;
  } catch { return null; }
}

export async function runScenario(id: string, opts?: { delay?: number; limit?: number }):
  Promise<{ ok: boolean; detail: string }> {
  try {
    const r = await fetch(`/api/simulator/scenarios/${encodeURIComponent(id)}/run`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(opts ?? {}), signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    const b = await r.json().catch(() => ({} as any));
    if (!r.ok) return { ok: false, detail: String(b.detail ?? `HTTP ${r.status}`) };
    return { ok: true, detail: `기동 — pid ${b.pid ?? "?"}` };
  } catch { return { ok: false, detail: "게이트 오프라인 — 목업 연출" }; }
}

export async function stopScenario(): Promise<boolean> {
  try {
    const r = await fetch("/api/simulator/stop", { method: "POST", signal: AbortSignal.timeout(TIMEOUT_MS) });
    return r.ok;
  } catch { return false; }
}

/** 수정 값 소프트 가드 — 최종 검증은 엔진(B)·게이트 몫, 프론트는 경고만 (차단 안 함)
 *  "+3.2%" → D6 ±3% / "+0.6σ" → A2 0.5σ */
export function modifyGuard(v: string): string | null {
  const m = v.trim().match(/^([-+]?\d+(?:\.\d+)?)\s*(%|σ|sigma)$/i);
  if (!m) return null;
  const n = Math.abs(parseFloat(m[1]));
  const unit = m[2] === "%" ? "%" : "σ";
  if (unit === "%" && n > 3) return `D6 상한(±3%) 초과 — 엔진 검증에서 반려될 수 있음`;
  if (unit === "σ" && n > 0.5) return `A2 상한(0.5σ) 초과 — 자동 적용 불가, 승인 재심 대상`;
  return null;
}

// ---- P6-4 백엔드 스택 관리 (S11 스택 패널) — GET /stack/status · POST /stack/start|stop ----
export interface StackService {
  name: string; enabled: boolean; running: boolean;
  pid: number | null; returncode: number | null;
  uptime_sec: number | null; last_line: string | null; new_console: boolean;
}

/** compose 컨테이너 상태 (2026-08-10) — 패널의 새 용도. 읽기 전용.
 *  `ok` = running 이고 health 가 healthy(또는 헬스체크 없음). restarting·exited·absent 는 false. */
export interface ComposeContainer {
  service: string; name: string; state: string; health: string;
  status: string; ok: boolean;
}

export interface StackSnapshot {
  services: StackService[];       // 호스트 자식 프로세스 (현재 빈 목록 — 전부 compose 이관)
  containers: ComposeContainer[]; // compose 컨테이너 — 실제로 봐야 하는 쪽
}

export async function stackStatus(): Promise<StackSnapshot | null> {
  try {
    const r = await fetch("/api/stack/status", { signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!r.ok) return null;
    const b = await r.json();
    return {
      services: Array.isArray(b?.services) ? b.services : [],
      containers: Array.isArray(b?.containers) ? b.containers : [],
    };
  } catch { return null; }
}

export async function stackStart(name?: string): Promise<boolean> {
  try {
    const r = await fetch("/api/stack/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(name ? { name } : {}), signal: AbortSignal.timeout(20000),
    });
    return r.ok;
  } catch { return false; }
}

export async function stackStop(name?: string): Promise<boolean> {
  try {
    const r = await fetch("/api/stack/stop", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(name ? { name } : {}), signal: AbortSignal.timeout(15000),
    });
    return r.ok;
  } catch { return false; }
}

/** 게이트웨이 자기 재시작 (2026-08-11) — 마운트 코드 리로드. compose `unless-stopped` 가 되살린다.
 *  응답 직후 프로세스가 죽으므로 fetch 실패(연결 끊김)도 "재시작 시작됨"으로 간주한다.
 *  ⚠️ 자식 시뮬레이터가 함께 죽는다 — 호출측(S11)은 시뮬 STOP 상태에서만 활성화할 것. */
export async function restartGateway(): Promise<boolean> {
  try {
    const r = await fetch("/api/stack/restart-gateway", {
      method: "POST", signal: AbortSignal.timeout(4000),
    });
    return r.ok;
  } catch { return true; }   // 연결 끊김 = 이미 죽는 중 = 성공으로 취급
}

// ── RTD 3단: 장비 정지 「제안」 상태 (TopBar 칩 + S1 배너 — 2026-08-04) ─────
// 경로① = reference_suspect 연속 K 롤업 (K 는 서버 params rtd_consecutive_k, 8/3 실측 기반 가안 18).
export interface RtdChamberRun { chamber_id: string; open_run: number; fired: boolean }
export interface RtdStatus {
  proposed: boolean;
  lane1: { k: number; chambers: RtdChamberRun[]; max_open_run: number; fired_chambers: string[] };
  lane2: { status: string; isolated_min: number; note: string };
  basis: string;
  scan_rollups: number;
  ts: string;
}
export async function fetchRtdStatus(): Promise<RtdStatus | null> {
  try {
    const r = await fetch("/api/rtd/status", { signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!r.ok) return null;
    return (await r.json()) as RtdStatus;
  } catch {
    return null;
  }
}

// ── S7 Qual 확정 + R9 재적격 실배선 (2026-08-10) ────────────────────────────
// 왜 지금까지 없었나: 두 라우트는 게이트웨이에 **처음부터 살아 있었다**(main.py:573·592).
// 막힌 건 읽기 쪽이었다 — `/quals/recent` 가 pm_count·incident_id 를 안 줘서 화면이
// 본문을 조립할 수 없었고, 그래서 S7 의 분기 3개와 하단 버튼이 로컬 상태·화면 이동만
// 하고 끝났다. 게이트웨이에 파생 2컬럼을 얹어 닫았다.
//
// ⚠️ 타임아웃은 TIMEOUT_MS(2.5s)가 아니라 8s 다. 두 호출은 LangGraph·SPC 하류를
//    깨우므로 승인 계열보다 느리다 — 2026-08-10 리허설 실측 `POST /requalify` **3.0초**
//    (14:37:30 → 14:37:33). 2.5s 를 쓰면 성공한 호출을 실패로 표시하고, 사용자가 다시
//    누르면 requalify 는 멱등이라 409 가 떠서 "이미 완료"로 보인다 — 최악의 조합.
const ACTION_TIMEOUT_MS = 8000;

/** 판정 확정 — POST /qual/verdict. verdict 는 4지표 proposed 를 따른다(사람이 뒤집지 않음).
 *  loud 는 incident_id 필수 (레짐 전환 Incident · 신규 결정 10) — 없으면 400 이므로 호출 전 검사. */
export interface QualVerdictPayload {
  qual_id: string;
  chamber_id: string;                 // 원형 그대로 (SIM_CH_4) — 표시형 CH4 아님
  verdict: "loud" | "quiet";
  pm_count: number;
  approved_by: string;
  incident_id?: string;
}

export async function postQualVerdict(p: QualVerdictPayload): Promise<GateResult> {
  if (p.verdict === "loud" && !p.incident_id) {
    return { live: false, detail: "요란 확정은 레짐 전환 Incident 가 필요합니다 — Incident 개설 대기" };
  }
  try {
    const r = await fetch("/api/qual/verdict", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(p), signal: AbortSignal.timeout(ACTION_TIMEOUT_MS),
    });
    const b = await r.json().catch(() => ({} as Record<string, unknown>));
    if (!r.ok) return { live: false, detail: `판정 확정 ${r.status} — ${String((b as any).detail ?? "오류")}` };
    return { live: true, detail: `QualVerdictConfirmed — ${p.verdict} 확정 · 가한계 Phase 진입` };
  } catch {
    return { live: false, detail: "게이트웨이 무응답 — 판정 미확정 (재시도 가능)" };
  }
}

/** R9 재적격 — POST /requalify/{incident_id}. **멱등**: 같은 Incident 재호출은 409.
 *  409 는 오류가 아니라 "이미 발행됨"이므로 완료로 표시한다 (헌법 §8-C 경계 1회). */
export interface RequalifyPayload {
  chamber_id: string;
  qual_id: string;
  pm_count: number;
  corrections_applied: string[];
  limit_version: string;
  approved_by: string;
}

export async function postRequalify(incidentId: string, p: RequalifyPayload): Promise<GateResult> {
  if (!incidentId) return { live: false, detail: "Incident 없음 — R9 대상 미확정" };
  try {
    const r = await fetch(`/api/requalify/${encodeURIComponent(incidentId)}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(p), signal: AbortSignal.timeout(ACTION_TIMEOUT_MS),
    });
    const b = await r.json().catch(() => ({} as Record<string, unknown>));
    if (r.status === 409) return { live: true, detail: "이미 발행됨 — Incident 당 1회 (멱등)" };
    if (!r.ok) return { live: false, detail: `R9 ${r.status} — ${String((b as any).detail ?? "오류")}` };
    return { live: true, detail: "ChamberRequalified — RTD inhibit 해제 하류 실행" };
  } catch {
    return { live: false, detail: "게이트웨이 무응답 — R9 미발행 (재시도 가능)" };
  }
}

// ── 처분 승인 요청 (2026-08-11) — S6 웨이퍼별 선택 → 처분 전용 카드 ──────────
// 8/10 까지는 정비 카드가 처분 카드를 겸해서, 승인 한 번이 정비 발행과 웨이퍼 확정을
// 동시에 했고 판정도 Incident 단위 1개 값이었다(장별 혼재 불가). 이제 선택을 그대로 실어
// 전용 카드를 연다. LLM 처분 Brief 생성이 붙어 있어 응답이 수 초 걸릴 수 있다 →
// 타임아웃을 액션 기본(8s)보다 넉넉히 준다.
const DISPO_REQUEST_TIMEOUT_MS = 30000;

export interface DispositionDecision {
  wafer_id: string;
  disposition: "RELEASE" | "SCRAP";
}

export interface DispositionRequestResult extends GateResult {
  counts?: Record<string, number>;
  llmOk?: boolean;
  confidence?: number | null;
}

/** 처분 카드 개설 — S6 는 **대상 웨이퍼만** 보낸다(체크박스). 판정은 에이전트 권고로 프리필돼
 *  카드에 실리고, 엔지니어가 승인 화면에서 근거를 보며 장별로 확정한다 (2026-08-11 PM 결정).
 *  판정까지 실어 보내려면 decisions 를 쓴다(스크립트·회귀 테스트 경로). */
export async function requestDisposition(
  incidentId: string, waferIds: string[], decisions?: DispositionDecision[],
): Promise<DispositionRequestResult> {
  if (!incidentId) return { live: false, detail: "Incident 없음 — 처분 대상 미확정" };
  if (!waferIds.length && !(decisions?.length)) {
    return { live: false, detail: "선택된 웨이퍼가 없습니다" };
  }
  try {
    const r = await fetch("/api/dispositions/request", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(decisions?.length
        ? { incident_id: incidentId, decisions, requested_by: APPROVER.approver }
        : { incident_id: incidentId, wafer_ids: waferIds, requested_by: APPROVER.approver }),
      signal: AbortSignal.timeout(DISPO_REQUEST_TIMEOUT_MS),
    });
    const b = await r.json().catch(() => ({} as Record<string, unknown>));
    if (!r.ok) {
      return { live: false, detail: `처분 요청 ${r.status} — ${String((b as any).detail ?? "오류")}` };
    }
    const counts = ((b as any).counts ?? {}) as Record<string, number>;
    const mix = Object.entries(counts).map(([k, v]) => `${k} ${v}장`).join(" · ");
    const llmOk = !!((b as any).llm?.ok);
    return {
      live: true, counts, llmOk, confidence: (b as any).confidence ?? null,
      // Brief 출처를 숨기지 않는다 — fallback 이면 화면에도 그렇게 적힌다(실값 위장 금지).
      detail: `처분 승인 요청 개설 — ${mix} (권고 프리필) · Brief ${llmOk ? `LLM conf ${Number((b as any).confidence ?? 0).toFixed(2)}` : "fallback(LLM 실패)"} — 판정은 승인 화면에서 확정`,
    };
  } catch {
    return { live: false, detail: "게이트웨이 무응답 — 처분 요청 미개설 (재시도 가능)" };
  }
}

// ── 회차 초기화 (2026-08-10) — A-2 를 버튼으로 ────────────────────────────
// 60초 이상 걸리므로 POST 는 **즉시** 돌아오고 진행은 status 폴링으로 본다.
// 절대 동기 대기로 바꾸지 말 것 — 어떤 타임아웃도 못 버틴다.
export interface DemoResetStatus {
  running: boolean;
  step: string | null;
  step_i: number;
  steps: string[];
  ok: boolean | null;              // null=진행 중 / true=완료 / false=중단
  lines: string[];
  started_at: string | null;
  elapsed_sec: number;
  detail: string;
  accepted?: boolean;              // POST 응답에만
}

export async function demoReset(): Promise<DemoResetStatus | null> {
  try {
    const r = await fetch("/api/demo/reset", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: "{}", signal: AbortSignal.timeout(ACTION_TIMEOUT_MS),
    });
    if (!r.ok) return null;
    return (await r.json()) as DemoResetStatus;
  } catch { return null; }
}

export async function demoResetStatus(): Promise<DemoResetStatus | null> {
  try {
    const r = await fetch("/api/demo/reset/status", { signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!r.ok) return null;
    return (await r.json()) as DemoResetStatus;
  } catch { return null; }
}

// ── CT⓪ 모델 보정 + 프라이어 (S2 「모델 보정」 카드 — 2026-08-10) ────────────
export interface Ct0Chamber {
  chamber_id: string; ct_id: string; residual: number | null;
  bias_applied: number | null; retrain_status: string; created_at: string;
}
export interface Ct0Status {
  mode: string;                    // off | shadow | active
  clamp: number; min_labels: number; decisions_total: number;
  chambers: Ct0Chamber[];
}
export async function fetchCt0Status(): Promise<Ct0Status | null> {
  try {
    const r = await fetch("/api/ct0/status", { signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!r.ok) return null;
    return (await r.json()) as Ct0Status;
  } catch { return null; }
}

/** 프라이어 시드 제안 — GET(읽기 전용). 실패·표본 부족도 화면이 정직하게 그리도록
 *  reason 을 그대로 넘긴다 (실값 위장 금지). */
export interface PriorProposal {
  proposal?: null; reason?: string;
  chamber?: string; seedBias?: number; marker?: string; basis?: string;
  delta?: number; basisMismatch?: boolean; basisNote?: string; estimate?: boolean;
}
export async function fetchPriorProposal(chamber: string): Promise<PriorProposal | null> {
  try {
    const r = await fetch(`/api/prior/proposal?chamber=${encodeURIComponent(chamber)}`,
                          { signal: AbortSignal.timeout(4000) });
    if (!r.ok) return { proposal: null, reason: `조회 실패 ${r.status}` };
    return (await r.json()) as PriorProposal;
  } catch { return null; }
}

/** R9 본문의 limit_version — 현행 active 관리선에서 읽는다. 가한계(provisional)가 있으면
 *  그것이 현행이므로 우선. 실패·빈값이면 "" 를 주고 호출부가 막는다 (빈 본문 전송 금지 —
 *  2026-08-09 내 스크립트가 빈 pm_count 로 422 를 받은 것과 같은 계열의 사고 방지). */
export async function fetchLimitVersion(chamber: string): Promise<string> {
  try {
    const r = await fetch(`/api/limits/active?chamber=${encodeURIComponent(chamber)}`,
                          { signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!r.ok) return "";
    const rows = ((await r.json())?.limits ?? []) as Array<Record<string, unknown>>;
    if (!rows.length) return "";
    const prov = rows.find((x) => x.trigger_type === "provisional");
    return String((prov ?? rows[rows.length - 1]).limit_version ?? "");
  } catch {
    return "";
  }
}
// ── S9 Agent Copilot: 자연어 질의 (2026-08-11 신설) ────────────────────────
// 계약: POST /agent/query — body {question, context?} / 응답 = CopilotAnswer
//   (단일 소스 = src/agent_service/app/schemas/copilot.py)
// 읽기 전용이다 — 이 경로로 승인·조치가 나가지 않는다(S9 원칙 ③).
// ⚠️ 서버 `EvidenceKind`(app/schemas/copilot.py)와 **같이 늘려야 한다** — 서버가 새 kind 를
//   보내는데 여기 없으면 타입만 거짓이 되고(런타임은 통과) 화면 분기가 조용히 빗나간다.
export type EvidenceKind =
  | "report" | "spc" | "chamber" | "knob" | "glossary" | "case" | "manual" | "knowledge";
export interface CopilotEvidence { kind: EvidenceKind; ref: string; label: string }
export interface CopilotAnswer {
  answer: string;
  evidence: CopilotEvidence[];
  deeplink: string | null;
  scope_note: string;
  /** true = 재료 부족으로 단정하지 않았음. 화면이 "근거 없음"을 표시하는 근거 */
  unsupported: boolean;
}
export interface CopilotContext {
  chamber?: string; incident_id?: string; sensor?: string; range?: string;
}

// 서버가 LLM 1콜을 태우므로 다른 라우트(2.5초)보다 넉넉해야 한다 — 조기 abort 는
// "서버는 답했는데 화면만 실패"를 만든다. 서버측 재료 조회는 3초에 자체 포기한다.
const COPILOT_TIMEOUT_MS = 40000;

/** 표시 키 → 정규 chamber_id (`CH3` → `SIM_CH_3`).
 *
 * 🔴 2026-08-11 실측: 뷰 모델이 목업 키(CH1~CH6)로 짜여 있고 라이브 피드를
 * `useLiveFdc.ts:152 chOf()` 가 `SIM_CH_3 → CH3` 로 **변환해서 조인**한다. 그 표시 키를
 * 그대로 서버에 보내면 DB 에 없는 값이라 조회가 0건이고, **모든 질문이 "근거 없음"** 이 된다.
 * 헌법 6-4 의 센서 원칙과 같다 — 표시 계층에서만 변환하고 **선을 넘는 값은 정규형**이어야 한다.
 */
export function canonicalChamber(display?: string): string | undefined {
  if (!display) return undefined;
  const m = /^CH(\d+)$/.exec(display.trim());
  return m ? `SIM_CH_${m[1]}` : display;   // 이미 정규형이면 그대로
}

/** 직전 대화 1턴 — 후속 질문("다른 챔버는?")의 맥락. 근거가 아니라 맥락 보조다. */
export interface CopilotTurn { question: string; answer: string }

export async function askCopilot(
  question: string, context: CopilotContext, history: CopilotTurn[] = [],
): Promise<CopilotAnswer> {
  context = { ...context, chamber: canonicalChamber(context.chamber) };
  try {
    const r = await fetch("/api/agent/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, context, history }),
      signal: AbortSignal.timeout(COPILOT_TIMEOUT_MS),
    });
    const body = await r.json().catch(() => null);
    if (!r.ok || !body || typeof body !== "object") {
      const detail = String((body as any)?.detail ?? `게이트 ${r.status}`);
      return { answer: `답변을 받지 못했습니다 — ${detail}`, evidence: [],
               deeplink: null, scope_note: "", unsupported: true };
    }
    return body as CopilotAnswer;
  } catch (e) {
    // 화면은 죽지 않는다. 실패도 "unsupported 인 답변"으로 렌더해 사용자가 재시도할 수 있게.
    const why = e instanceof DOMException && e.name === "TimeoutError" ? "응답 시간 초과" : "연결 실패";
    return { answer: `답변을 받지 못했습니다 — ${why}.`, evidence: [],
             deeplink: null, scope_note: "", unsupported: true };
  }
}
