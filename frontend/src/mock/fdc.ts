// AITCH S1 목업 데이터 (베이스) — 계약/DB 문법(INC-ID·C코드·lifecycle) 준수.
// 라이브 흐름은 src/live/useLiveFdc.ts 가 이 베이스 위에 노이즈/스트림을 얹는다.
// 실배선 시: useLiveFdc 내부를 WebSocket/API 구독으로 교체.
// 세계관: Etch 클러스터 장비 1대 = 챔버(PM) 4개 (시뮬 스펙 v1.3, 멘토 확인 2026-07-20).
//   CH1 정상 / CH2 드리프트 warning (Recipe 시나리오) / CH3 critical 주인공 / CH4 PM→Qual 요란→가한계 (레짐)

export type Severity = "normal" | "warning" | "critical" | "provisional" | "pm";
export type QueueStatus = "reopen" | "analyzing" | "pending" | "verifying" | "closed";

export interface ShapItem {
  name: string;   // 표시명 (sensor_map)
  code: string;   // C코드 (헌법 6-4)
  val: number;
  pct: number;
}

export interface Chamber {
  id: string;
  /** 소속 장비 (세계관: Etch 클러스터 1대 = 4PM — 시뮬 스펙 v1.3) */
  tool: string;
  sev: Severity;
  recipe: string;
  rev: number;
  alarmText: string | null;
  /** 수치 베이스 (PM 챔버는 null) */
  sensors: { c11: number; c32: number; c17: number; c4: number } | null;
  c4Alarm: boolean;
  drift: number | null;
  c65: number | null;
  shap: ShapItem[];
  nelson: string | null;
  /** UCL(+3σ) y좌표 — 곡선·초기 이력은 라이브 레이어가 σ 레벨(drift)에서 합성 */
  ucl: number;
  /** LIVE 헤드 툴팁 표시 (critical 챔버) — Nelson 밴드는 라이브에서 UCL 위반 연속 구간으로 자동 산출 */
  liveMarker?: boolean;
  /** 장비 가동 상태 (표시용) */
  runState: "RUN" | "IDLE" | "PM";
  /** SPC 차트의 관리 대상 — Chamber×Recipe×Step 관리 단위 명시 (센서 · 스텝 · 윈도우) */
  spcLabel: string;
  /** 현행 관리선 버전 · 최근 재설정일 */
  limitVer: string;
  /** C65 예측 신뢰도 (0~1, PM은 null) */
  predConf: number | null;
  deltaLabel: string;
  /** 칩 보조표기 강제 (PROV/PM 등 비수치) */
  tagOverride?: string;
}

export interface Incident {
  id: string;
  chamber: string;
  sev: "critical" | "warning";
  title: string;
  alarms: number;
  ageMin: number;           // 경과(분) — 라이브에서 +1/분
  status: QueueStatus;
  owner: { name: string; color: string } | null;
  /** Context Score (게이트 3중화: ≥31 자동 / critical 즉시 / 수동) */
  score: number;
  /** 레짐 전환 Incident (BL1 incident_type) */
  regime?: boolean;
  /** Supervisor 4지선다 판정 — HEAD판 S4(처분 카드·승인 그래프) 이식 정합 (2026-08-12) */
  verdict?: string | null;
}

export const chambers: Chamber[] = [
  {
    id: "CH1", tool: "ETCH-01", sev: "normal", recipe: "OX-ETCH-114", rev: 4, alarmText: null,
    sensors: { c11: -63.8, c32: 2, c17: 60.9, c4: 421.0 }, c4Alarm: false,
    drift: 0.41, c65: 941,
    shap: [
      { name: "DC bias", code: "C11", val: 0.18, pct: 36 },
      { name: "Gas flow", code: "C4", val: 0.12, pct: 24 },
    ],
    nelson: null,
    ucl: 58, deltaLabel: "-0.1σ TODAY · C11 DC BIAS",
    runState: "RUN", spcLabel: "C11 · STEP 4 · SETTLED", limitVer: "v2 · 07/01", predConf: 0.92,
  },
  {
    id: "CH2", tool: "ETCH-01", sev: "warning", recipe: "OX-ETCH-114", rev: 4, alarmText: null,
    sensors: { c11: -65.6, c32: 4, c17: 62.8, c4: 417.1 }, c4Alarm: false,
    drift: 2.13, c65: 1152,
    shap: [
      { name: "DC bias", code: "C11", val: 0.31, pct: 62 },
      { name: "RF reflect", code: "C32", val: 0.19, pct: 38 },
    ],
    nelson: null,
    ucl: 58, deltaLabel: "+0.3σ TODAY · C11 DC BIAS",
    runState: "RUN", spcLabel: "C11 · STEP 4 · SETTLED", limitVer: "v2 · 07/05", predConf: 0.86,
  },
  {
    id: "CH3", tool: "ETCH-01", sev: "critical", recipe: "OX-ETCH-114", rev: 4,
    alarmText: "⚠ ALARM · C4 GAS FLOW",
    sensors: { c11: -64.9, c32: 8, c17: 61.4, c4: 418.2 }, c4Alarm: true,
    drift: 3.42, c65: 1284,
    shap: [
      { name: "Gas flow", code: "C4", val: 0.41, pct: 82 },
      { name: "DC bias", code: "C11", val: 0.27, pct: 54 },
    ],
    nelson: "N2 · 9점 연속 편향 · C4",
    ucl: 58, liveMarker: true,
    deltaLabel: "+0.4σ TODAY · C4 GAS FLOW",
    runState: "RUN", spcLabel: "C4 · STEP 4 · SETTLED", limitVer: "v3 · 07/12", predConf: 0.78,
  },
  {
    id: "CH4", tool: "ETCH-01", sev: "provisional", recipe: "OX-ETCH-114", rev: 4, alarmText: null,
    sensors: { c11: -64.4, c32: 3, c17: 61.2, c4: 420.3 }, c4Alarm: false,
    drift: 1.8, c65: 1021,
    shap: [
      { name: "Gas flow", code: "C4", val: 0.16, pct: 32 },
      { name: "Chamber temp", code: "C17", val: 0.11, pct: 22 },
    ],
    nelson: null,
    ucl: 58, deltaLabel: "가한계 Phase 1 · 광폭 관리선",
    tagOverride: "PROV",
    runState: "RUN", spcLabel: "C4 · STEP 3 · SETTLED", limitVer: "PROV P1", predConf: 0.81,
  },
];

export const incidents: Incident[] = [
  {
    id: "INC-20260713-CH3-001", chamber: "CH3", sev: "critical",
    title: "실력치 재설정 권고 — C11 기준선 노후", alarms: 4, ageMin: 23,
    status: "reopen", owner: { name: "박", color: "#33739c" }, score: 72,
  },
  {
    id: "INC-20260714-CH3-002", chamber: "CH3", sev: "critical",
    title: "RF 반사파 급등 — 매칭 네트워크 열화 의심", alarms: 2, ageMin: 8,
    status: "analyzing", owner: null, score: 58,
  },
  {
    id: "INC-20260714-CH2-001", chamber: "CH2", sev: "warning",
    title: "드리프트 2.1σ — 다음 로트 전 시뮬 권고", alarms: 1, ageMin: 41,
    status: "pending", owner: { name: "이", color: "#2fc9a7" }, score: 44,
  },
  {
    id: "INC-20260714-CH4-001", chamber: "CH4", sev: "warning",
    title: "Qual 요란 — 가한계 진입 · Phase 2 재산정 대기", alarms: 3, ageMin: 6,
    status: "pending", owner: null, score: 38, regime: true,
  },
  {
    id: "INC-20260713-CH1-004", chamber: "CH1", sev: "warning",
    title: "C32 센서 노이즈 증가 — 케이블 점검", alarms: 1, ageMin: 125,
    status: "pending", owner: { name: "김", color: "#e8b25c" }, score: 33,
  },
];

/** 전체 인시던트 (S3) — 열린 5건 + 종결·검증 이력 */
export const incidentsAll: Incident[] = [
  ...incidents,
  {
    id: "INC-20260713-CH2-003", chamber: "CH2", sev: "warning",
    title: "C17 온도 미세 상승 — 관찰 종결", alarms: 1, ageMin: 1420,
    status: "closed", owner: { name: "박", color: "#33739c" }, score: 24,
  },
  {
    id: "INC-20260712-CH3-007", chamber: "CH3", sev: "critical",
    title: "C11 실력치 +4.1% 재설정 — 적용 후 감시 중", alarms: 5, ageMin: 2870,
    status: "verifying", owner: { name: "이", color: "#2fc9a7" }, score: 81,
  },
];

/** S4 Approval Brief — Supervisor 4지선다 산출물 */
/** Supervisor 4지선다(계약 `verdict`) → 표시용 한글. 코드·DB·계약은 영문 enum 유지
 *  (헌법 6-4 — 변환은 표시 계층에서만). 목록·체인이 같은 낱말을 쓰도록 한 곳에 둔다. */
export const VERDICT_KO: Record<string, string> = {
  equipment_fault: "진짜 장비 이상",
  process_shift: "공정 조건 이탈",
  baseline_aging: "기준선 노후",
  escalate: "에스컬레이션",
  // 처분 전용 카드 (2026-08-11, 86b1bd6 이식) — 판정문 자리에 "웨이퍼 처분 심사"가 온다.
  wafer_disposition: "웨이퍼 처분 심사",
};

export interface BriefOption {
  key: "recipe" | "limit" | "manual";
  title: string;
  action: string;
  feasibility: string;
  confidence: number;
  rejected?: string;
  /** 기각은 아니지만 유보·경고성 사유 (예: escalate_reason "가한계 Phase 대기") — 흐림 없음 */
  note?: string;
}
export interface Brief {
  verdict: "equipment_fault" | "process_shift" | "baseline_aging" | "escalate";
  verdictKo: string;
  reason: string;
  confidence: number;
  evidence: string[];   // 정확히 3
  counter: string;      // 정확히 1
  /** undefined = none_executable(4지선다 ④) — 추천 배지·하이라이트 없음 (2026-07-31) */
  recommended?: BriefOption["key"];
  options: BriefOption[];
  dispo: { held: number; rec: "RELEASE" | "SCRAP"; reason: string };
}
export const briefs: Record<string, Brief> = {
  "INC-20260713-CH3-001": {
    verdict: "baseline_aging", verdictKo: "기준선 노후",
    reason: "PM 직후 정상 상태 이동 + 과거 유사 사례(sim 0.91) 일치 — 진성 열화 증거 부족",
    confidence: 0.88,
    evidence: [
      "C33 리셋 12 wafer 후 C11 중심 이동 +4.1% — 레짐 전환 패턴",
      "CASE-0912 유사도 0.91 — '실력치 +4.0% 재설정, 가성알람 62% 감소, 미탐 0'",
      "AE anomaly 정상 대역 (0.021 < P95 0.05) — 장비 이상 신호 부재",
    ],
    counter: "N2 9점 연속 편향이 재설정 후에도 2회 재발한 이력 — 재발 시 정비 경로 재평가",
    recommended: "limit",
    options: [
      { key: "limit", title: "실력치 재설정", action: "C11 중심 +4.1% (v3→v4) 재산정", feasibility: "HIGH · 다운타임 없음", confidence: 0.88 },
      { key: "recipe", title: "Recipe 튜닝", action: "C1 RF Power −1.2%", feasibility: "MID · 검증 30 wafer", confidence: 0.55, rejected: "SHAP 상위가 손잡이 축 아님 — 레짐 신호(C17) 우세" },
      { key: "manual", title: "정비", action: "매칭 네트워크 점검", feasibility: "LOW · 8h 정지", confidence: 0.42, rejected: "급변·anomaly 부재 — 장비 이상 신호 없음" },
    ],
    dispo: { held: 4, rec: "RELEASE", reason: "C65 예측 정상 범위 (CONF 0.86)" },
  },
  "INC-20260714-CH3-002": {
    verdict: "equipment_fault", verdictKo: "진짜 장비 이상",
    reason: "C32 반사파 급등 + AE anomaly P99 초과 — 매칭 네트워크 열화 패턴과 일치",
    confidence: 0.79,
    evidence: [
      "C32 Reflected Power 3.8σ 급등 — 10 wafer 내 계단 변화",
      "AE 재구성 오차 0.081 (P99.5 초과) — 다변량 규칙 붕괴",
      "EM-114 §4.2 매칭 네트워크 열화 증상표와 3/4 일치",
    ],
    counter: "C11 bias는 정상 대역 유지 — 열화 초기 단계일 가능성",
    recommended: "manual",
    options: [
      { key: "manual", title: "정비", action: "매칭 네트워크 검사 + Focus Ring 확인", feasibility: "LOW · 8h 정지", confidence: 0.79 },
      { key: "limit", title: "실력치 재설정", action: "C32 한계 완화", feasibility: "HIGH", confidence: 0.31, rejected: "진성 열화를 기준선 이동으로 가리게 됨 (A2 취지 위반)" },
      { key: "recipe", title: "Recipe 튜닝", action: "—", feasibility: "—", confidence: 0.18, rejected: "공정 조건 이탈 증거 없음" },
    ],
    dispo: { held: 6, rec: "SCRAP", reason: "급등 구간 2 wafer — C65 예측 P99 초과" },
  },
  "INC-20260714-CH2-001": {
    verdict: "process_shift", verdictKo: "공정 조건 이탈",
    reason: "C4 가스 유량이 설정 대비 −1.8% 지속 편차 — SHAP 1위 손잡이 축",
    confidence: 0.72,
    evidence: [
      "C4 실측−설정 편차 −1.8% 지속 (40 wafer)",
      "SHAP 1위 C4 (기여 0.41) — 손잡이 매핑 축 (C6-3)",
      "Process KB tuning_axis 102 — 유사 조건 튜닝 사례 개선 83%",
    ],
    counter: "드리프트 2.1σ는 warning 대역 — 관리선 내 자연 변동 가능성 잔존",
    recommended: "recipe",
    options: [
      { key: "recipe", title: "Recipe 튜닝", action: "C4 setpoint +1.8% 복원", feasibility: "HIGH · 즉시 반영", confidence: 0.72 },
      { key: "limit", title: "실력치 재설정", action: "—", feasibility: "—", confidence: 0.28, rejected: "설정-실측 갭은 기준선 문제가 아님" },
      { key: "manual", title: "정비", action: "MFC 점검", feasibility: "MID", confidence: 0.35, rejected: "편차가 안정적 — 하드웨어 고장 패턴 아님" },
    ],
    dispo: { held: 2, rec: "RELEASE", reason: "예측 정상 범위 · 튜닝 후 검증 예정" },
  },
};

/** S4 시스템 제안 ① — 마커 개정 트랙 (모델 갱신 원칙 4호 준용 · 자동 개정 금지)
 *  값 = 2026-08-09 `prior_ledger.py refit/propose` 실측 (블록 61): 자(basis) 규율이
 *  scenario_def 5건을 refit 에서 격리 → 자 일치 0/3건으로 **개정 정직 보류**.
 *  메커니즘 적합(1.23%)은 회로 검증 전용 — v1 재캘리에 쓰면 8/8 "72%→1.23%" 사고 재발. */
export interface MarkerProposal {
  id: string;
  kind: "prior_marker";
  title: string;
  ledgerN: number;                 // 전이 원장 축적 건수 (자 불일치 포함 전체)
  ledgerMatched: number;           // 자 일치 건수 — 개정 자격은 이 숫자만 (임계 3)
  current: string;
  proposed: string;
  candidates: { marker: string; errPct: number; note?: string }[];
  /** 프라이어 오차 |추정−실측|/실측 (%) — 회차별 추이. 자 일치 0건이라 산출 불가 = [] */
  errTrend: number[];
  counter: string;
  guard: string;
}
export const markerProposal: MarkerProposal = {
  id: "PRIOR-REVISE-TRACK", kind: "prior_marker",
  title: "프라이어 마커 개정 — 보류 (자 일치 원장 0/3건)",
  ledgerN: 5, ledgerMatched: 0,
  current: "v1 · C17 slope −6.96 · 자 qual5-seg1→last1000_mean (실측 전이 1건 앵커)",
  proposed: "개정 없음 — scenario_def 5건은 자 불일치로 refit 제외 (메커니즘 검증 전용)",
  candidates: [
    { marker: "C17 재캘리 (v1 채널 유지)", errPct: 1.23, note: "자 불일치 — v1 재캘리 사용 금지" },
    { marker: "C11_min 단독 (전기 축)", errPct: 1.24, note: "자 불일치 — 동상" },
  ],
  errTrend: [],
  counter: "원장 5건 전부 시뮬 세계(scenario_def) — 자 일치 그룹이 비어 개정 불가. C17+C11 조합은 공선(조건수 16,596>30) 기각. 시뮬 세계는 결정론적 직선이라 2건에 수렴 — 낮은 오차가 곧 근거가 아니다 (주입 공개 원칙)",
  guard: "개정은 자 일치(qual5-seg1→last1000) 실전이 ≥3건 축적 후 — prior_markers 새 버전 INSERT·승인 경유·롤백 = 직전 버전 재활성화 (물리 미개입 — correction 미발행)",
};

/** S4 시스템 제안 ② — 프라이어 시드 (R9 "신규 기준선 수립" · 승인 1회 경유 — clamp 예외)
 *  값 = 2026-08-09 `GET /prior/proposal?chamber=SIM_CH_4` 실측 (블록 62). 승인 시
 *  POST /prior/seed → approval_records 기록 + bias_<chamber>.json 원자 쓰기 (#152 계약 동형).
 *  mode=off 고정 — active 전환은 P2 Scorecard 게이트 실증 뒤 (CT⓪ §13). */
export interface SeedProposal {
  id: string;                      // = 원장 전이 ID (알림 ref 딥링크와 동일)
  kind: "prior_seed";
  title: string;
  chamber: string;                 // 표시 챔버 (월드 매핑)
  chamberRef: string;              // 원장 chamber_id — bias 파일명이 이 값을 쓴다
  transitionId: string;
  marker: string;
  delta: number;
  deltaBasis: string;
  basisMismatch: boolean;
  basisNote: string;
  seedBias: number;                // 점근 상수 — bias_<chamber>.json 의 `bias` (서빙 가산 후보)
  template: { tauDays: number; amplitude: number; note: string };
  ledger: { n: number; injected: number };
  refActual: number | null;        // 직전 유사 전이 실측 점근 (대조용)
  estimate: true;                  // '추정' 딱지 (신규 결정 9 — 필수)
  guard: string;
}
export const seedProposal: SeedProposal = {
  id: "PRI-20260629-SIMCH4-116",
  kind: "prior_seed",
  title: "프라이어 시드 — CH4 요란 D+0 기준선 (+157.4 '추정')",
  chamber: "CH4", chamberRef: "SIM_CH_4",
  transitionId: "PRI-20260629-SIMCH4-116",
  marker: "v1 · slope -6.96",
  delta: -22.62, deltaBasis: "scenario_def",
  basisMismatch: true,
  basisNote: "시뮬 전이(scenario_def)에 실자 마커 적용 — 메커니즘 시연 지위 (주입 공개 원칙)",
  seedBias: 157.4,
  template: { tauDays: 12.5, amplitude: 491.0, note: "온셋 과도 몫 — 서빙 계약(상수 1개)에는 미반영, 표시·가한계 폭 용" },
  ledger: { n: 5, injected: 1 },
  refActual: 553.0,
  estimate: true,
  guard: "적용은 승인 1회 경유(R9 — clamp 예외) · mode=active 는 P2 Scorecard 게이트 뒤 · 요란 리셋 시 updater 가 재계산으로 인수(D+60)",
};

/** S6 Disposition — HOLD wafer */
export interface DispoRow {
  wafer: string; lot: string; chamber: string;
  pred: number; conf: number; incident: string;
  rec: "RELEASE" | "SCRAP"; reason: string;
}
export const dispositions: DispoRow[] = [
  { wafer: "W26-0714-1189", lot: "L230", chamber: "CH3", pred: 1284, conf: 0.78, incident: "INC-20260714-CH3-002", rec: "SCRAP", reason: "급등 구간 · P99 초과" },
  { wafer: "W26-0714-1190", lot: "L230", chamber: "CH3", pred: 1231, conf: 0.76, incident: "INC-20260714-CH3-002", rec: "SCRAP", reason: "급등 구간 · P99 초과" },
  { wafer: "W26-0714-1191", lot: "L230", chamber: "CH3", pred: 1102, conf: 0.81, incident: "INC-20260714-CH3-002", rec: "SCRAP", reason: "AE anomaly 동반" },
  { wafer: "W26-0714-1192", lot: "L230", chamber: "CH3", pred: 987, conf: 0.83, incident: "INC-20260714-CH3-002", rec: "RELEASE", reason: "예측 정상 · 여유 1.2σ" },
  { wafer: "W26-0714-1193", lot: "L231", chamber: "CH3", pred: 942, conf: 0.85, incident: "INC-20260714-CH3-002", rec: "RELEASE", reason: "예측 정상" },
  { wafer: "W26-0714-1194", lot: "L231", chamber: "CH3", pred: 918, conf: 0.86, incident: "INC-20260714-CH3-002", rec: "RELEASE", reason: "예측 정상" },
  { wafer: "W26-0714-1168", lot: "L229", chamber: "CH3", pred: 1051, conf: 0.74, incident: "INC-20260713-CH3-001", rec: "RELEASE", reason: "기준선 노후 판정 — 실측 정상 이동" },
  { wafer: "W26-0714-1169", lot: "L229", chamber: "CH3", pred: 1033, conf: 0.75, incident: "INC-20260713-CH3-001", rec: "RELEASE", reason: "기준선 노후 판정" },
  { wafer: "W26-0714-1170", lot: "L229", chamber: "CH3", pred: 1021, conf: 0.77, incident: "INC-20260713-CH3-001", rec: "RELEASE", reason: "기준선 노후 판정" },
  { wafer: "W26-0714-1171", lot: "L229", chamber: "CH3", pred: 1008, conf: 0.79, incident: "INC-20260713-CH3-001", rec: "RELEASE", reason: "기준선 노후 판정" },
  { wafer: "W26-0714-1201", lot: "L231", chamber: "CH2", pred: 1149, conf: 0.86, incident: "INC-20260714-CH2-001", rec: "RELEASE", reason: "warning 대역 · 튜닝 후 검증" },
  { wafer: "W26-0714-1202", lot: "L231", chamber: "CH2", pred: 1153, conf: 0.86, incident: "INC-20260714-CH2-001", rec: "RELEASE", reason: "warning 대역" },
];

/** S7 Qual — PM 후 판정 */
export const qualData = {
  chamber: "CH4", pm: "대PM · Focus Ring 교체", openedAt: "09:40", phase: 1 as 0 | 1 | 2,
  wafers: [
    { id: "QUAL-01", c65: 2911, done: true },
    { id: "QUAL-02", c65: 2874, done: true },
    { id: "QUAL-03", c65: 2842, done: true },
    { id: "QUAL-04", c65: 2799, done: true },
    { id: "QUAL-05", c65: 2781 as number | null, done: true },
  ],
  metrics: [
    { k: "AE 재구성 오차", v: "0.074", limit: "< 0.050", pass: false },
    { k: "σ-갭 (스냅샷 대비)", v: "2.6σ", limit: "< 2.0σ", pass: false },
    { k: "Nelson 위반", v: "1건 (N3)", limit: "0건", pass: false },
    { k: "C65 proxy 편차", v: "+418", limit: "< +150", pass: false },
  ],
  verdict: "요란" as "조용" | "요란" | "진행중",
};

/** S8 KPI */
export const kpiExt = {
  pareto: [
    // 라이브 표시명(`live/useKpi.ts` SENSOR_LABEL)과 같은 문자열 — 다르면 게이트웨이가
    //   뜨는 순간 폴백→라이브 전환에서 **글자가 바뀌어** 깜빡인 것처럼 보인다.
    { k: "C4 GAS FLOW", n: 9 }, { k: "C11 DC BIAS", n: 6 }, { k: "C17 TEMP", n: 4 }, { k: "C32", n: 2 },
  ],
  chambers: [
    { id: "CH3", n: 11 }, { id: "CH4", n: 4 }, { id: "CH2", n: 3 }, { id: "CH1", n: 2 },
  ],
  score: [
    { id: "M2", label: "미탐 (심각 놓침)", v: "0건", pass: true },
    { id: "M5", label: "가성알람 감소", v: "−62%", pass: true },
    { id: "M8", label: "Scorecard 미탐 가드", v: "0건", pass: true },
    { id: "M12", label: "라우팅 커버리지", v: "94%", pass: false },
    { id: "M13", label: "중복 승인", v: "0건", pass: true },
  ],
  trend: [4, 7, 3, 9, 12, 6, 5],
};

/** S11 시뮬레이터 — 실제 시나리오 라이브러리 (src/simulator/scenarios) */
export interface Scenario {
  id: string; name: string; desc: string; target: string; tag: string;
}
export const scenarios: Scenario[] = [
  { id: "drift_c11", name: "Drift — C11 계단 이동", desc: "+2.5σ drift, 정착 구간 주입 → 시나리오 1 (실력치 루프)", target: "CH3", tag: "시나리오 1" },
  { id: "qual_loud", name: "Qual 요란 — 대PM 후 레짐 전환", desc: "PM → Qual 5장 → 4지표 판정 → 가한계 → 재학습", target: "CH4", tag: "시나리오 2" },
  { id: "recipe_c4", name: "Recipe 이탈 — C4 유량", desc: "C4 −1.8% 이탈 → 튜닝안 → 승인 → setpoint 복원 검증", target: "CH2", tag: "시나리오 3" },
  { id: "multi_chamber_common_c17", name: "다챔버 동시 이탈 — C17", desc: "3/4 챔버 +3.0σ — TTTM 역방향(참조 의심) 검증", target: "CH1·2·4", tag: "엣지" },
  { id: "spike_c32", name: "Spike — C32 단발 피크", desc: "단일 wafer 스파이크 — N1 즉발 검증", target: "CH1", tag: "엣지" },
  { id: "storm", name: "Alert Storm", desc: "다센서 동시 알람 — Incident 병합·중복 승인 0 검증", target: "CH3", tag: "엣지" },
];

/** 알림 벨 — 이벤트 피드 (푸시: 상태 전이만. 결정 대기 스냅샷은 Decision Queue 몫) */
export interface NotifItem {
  at: string;
  kind: "incident" | "reopen" | "verifying" | "qual" | "sla" | "system";
  text: string;
  sub: string;
  ref: string | null; // incident_id — 있으면 S4로
}
export const notifications: NotifItem[] = [
  { at: "방금", kind: "qual", text: "CH4 Qual 판정 — 요란 (4지표 FAIL)", sub: "레짐 전환 Incident 개설 · 가한계 진입", ref: "INC-20260714-CH4-001" },
  { at: "12m", kind: "system", text: "SYSTEM 제안 — 프라이어 시드 (+157.4 '추정')", sub: "요란 전이 D+0 · 원장 5건(주입 1) · R9 승인 1회 경유 · mode=off", ref: "PRI-20260629-SIMCH4-116" },
  { at: "3m", kind: "sla", text: "CRITICAL SLA 초과 — C11 기준선 노후", sub: "REOPEN 후 30분 경과 · 재승인 대기", ref: "INC-20260713-CH3-001" },
  { at: "8m", kind: "incident", text: "신규 Incident — RF 반사파 급등 (CH3)", sub: "critical 즉시 게이트 · 리포트 수합 중", ref: "INC-20260714-CH3-002" },
  { at: "23m", kind: "reopen", text: "REOPEN — 실력치 재설정 권고", sub: "반려 → 재분석 완료", ref: "INC-20260713-CH3-001" },
  { at: "1h", kind: "verifying", text: "적용 후 감시(reopen 창) — C11 +4.1% 재설정", sub: "23/30 wafer · 재발 0 · 소급 채점 오탐 −62%·미탐 0 (승인 근거)", ref: null },
  { at: "6m", kind: "incident", text: "신규 Incident — Qual 요란·가한계 진입 (CH4)", sub: "score 38 · 자동 게이트 · REGIME", ref: "INC-20260714-CH4-001" },
];

/** Settings — 파라미터 스냅샷 (정본: config/params.yaml · 합의안 v1) */
export const paramsView = [
  { k: "A1 관리한계 폭", v: "±3.0σ" }, { k: "A2 1회 재설정 상한", v: "0.5σ" },
  { k: "A3 누적 상한(사이클)", v: "1.0σ" }, { k: "A4 rolling window", v: "N=500" },
  { k: "A9 정기 리캘리 주기", v: "500 wafer" }, { k: "A10 dead band", v: "2×SE" },
  { k: "B1 TTTM 경고/심각", v: "2.0σ / 3.0σ" }, { k: "B3 역방향 룰", v: "≥ 3/4" },
  { k: "B5 Incident 병합창", v: "20 wafer" }, { k: "B7 Agent 가동 임계", v: "score ≥ 31" },
  { k: "TTTM window / min_fill", v: "20 / 10" }, { k: "D6 recipe delta 상한", v: "±3%" },
];

export const varStats = {
  amount: "$214,000", hold: 12, scrap: 3, verify: 9,
  basis: "HOLD 12 wafer × 평균 wafer 가치 ≈ $17.8K (C65 예측 손실 환산)",
};

/** 조치 & 효과 — 승인 게이트 통과 후 적용된 조치와 검증 상태 (closed loop의 '검증' 단계) */
export interface ActionItem {
  id: string;
  type: "LIM" | "RCP" | "MNT";
  title: string;
  chamber: string;
  at: string;
  status: "applied" | "verifying" | "confirmed";
  /** 핵심 효과 지표 (검증 결과) */
  metric: string;
  /** 적용 후 감시(reopen 창)/검증 진행 wafer (done, total) — 없으면 바 미표시. 섀도(소급 채점)는 승인 전 근거라 진행바 없음 */
  progress?: [number, number];
}
export const actions: ActionItem[] = [
  {
    id: "LIM-20260714-CH3-002", type: "LIM", title: "C11 실력치 +4.1% 재설정",
    chamber: "CH3", at: "10:24", status: "verifying",
    metric: "재발 0 · 소급 채점 오탐 -62%·미탐 0", progress: [23, 30],
  },
  {
    id: "RCP-20260713-CH2-001", type: "RCP", title: "C1 RF Power -1.3% 튜닝",
    chamber: "CH2", at: "어제 16:02", status: "confirmed",
    metric: "C65 예측 -9%", progress: [30, 30],
  },
  {
    id: "MNT-20260712-CH4-001", type: "MNT", title: "Focus Ring 교체 + PM",
    chamber: "CH4", at: "09:40", status: "applied",
    metric: "Qual 요란 판정 — 가한계 Phase 1 진입",
  },
];

/** 플릿 전체 웨이퍼 스트림 1건 — ch = 해당 wafer를 처리한 챔버 (시뮬레이터 라운드로빈), lot = 소속 LOT */
export interface PulseItem { wafer: string; c65: number; warn: boolean; ch: string; lot: string }
export const initialPulse: PulseItem[] = [
  { wafer: "W26-0714-1201", c65: 1148, warn: false, ch: "CH2", lot: "L231" },
  { wafer: "W26-0714-1200", c65: 947, warn: false, ch: "CH1", lot: "L231" },
  { wafer: "W26-0714-1199", c65: 1421, warn: true, ch: "CH3", lot: "L230" },
  { wafer: "W26-0714-1198", c65: 1017, warn: false, ch: "CH4", lot: "L230" },
  { wafer: "W26-0714-1197", c65: 938, warn: false, ch: "CH1", lot: "L230" },
];
export const pulseNextNo = 1202;

export const kpis = { today: 12, avgResp: "18m", miss: "0%" };

// 2026-08-11 삭제 — S9 Copilot 실배선(`POST /api/agent/query`)으로 대체됐다.
//   여기 있던 것은 고정 문자열이었다: "CH3 RF 반사파 급등 — 기준선 노후 1위(0.78)…" +
//   근거 카드 2장(CASE-0912 · EM-114). **DB 에 없는 ID 였다** — 실배선 후에는 서버
//   인용 가드를 통과한 실존 ID 만 온다. 되살리지 말 것.

// 2026-08-03 정직화 (PM 상황판 §3 ① · ⑦).
// cycle: "CYCLE 424" — 어디서도 산출되지 않는 상수였다. SideNav 가 이걸 실 LAG 옆에
//   나란히 그려서 "라이브 카운터" 처럼 보였다. 산출 근거가 없으므로 삭제한다.
// versions: "AE v3" — models/anomaly_ae/ 에는 ae_v1 뿐이다(2026-08-03 실측).
//   같은 화면 SettingsScreen 의 "AE v1" 과 정면 모순이었고, 없는 모델을 광고하고 있었다.
//   XGB v2 는 실재한다 — models/c65_predictor/v2_lean85/lean85_20260720_163040_initial.
export const site = {
  fab: "FAB A", org: "SK HYNIX", tool: "ETCH-01",
  /** `line`·`bay` 는 인트로(ToolIntro) 상단 계층 표기용 — 팹이 커져도 화면을 다시 짜지
   *  않도록 회사›팹›라인›베이까지 명시한다. 2026-08-13 인트로 이식분. */
  line: "LINE 3", bay: "BAY 12",
  versions: "XGBoost v2 (lean-85) · AE v1",
};

export const fmtAge = (m: number): string =>
  m < 60 ? `${m}m` : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`;