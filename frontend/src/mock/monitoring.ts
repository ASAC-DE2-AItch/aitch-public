// S1 목업 데이터 — API Contract(fdc.prediction) 스키마 그대로.
// W4에서 fetch("/api/dashboard/summary") 한 줄로 교체 (개발 가이드: 더미 → 구독 교체)

export type ChamberStatus = "NORMAL" | "WARNING" | "CRITICAL" | "INHIBIT" | "PM_QUAL";

export interface ChamberSummary {
  chamberId: string;
  status: ChamberStatus;
  statusNote: string;
  c65Series: number[];      // 최근 60 wafer 예측 추이
  driftScore: number;       // 0~1 (B4 threshold 0.7)
  anomalyScore: number;     // 0~1 (C6 threshold 0.2)
  holdCount: number;
  rfProgress: number;       // 0~1 — PM 사이클 내 위상 (C33 기반)
}

export interface WaferRow {
  waferId: string;
  chamberId: string;
  predictedC65: number;
  risk: "low" | "mid" | "high";
  shapTop3: { sensor: string; contribution: number }[];
  spcFlags: { sensor: string; rule: string }[];
  ts: string;
}

// 시드 고정 의사난수 (렌더마다 안 바뀌게)
function mulberry32(seed: number) {
  return () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function series(rand: () => number, base: number, noise: number, drift = 0): number[] {
  const out: number[] = [];
  for (let i = 0; i < 60; i++) {
    out.push(Math.round(base + (rand() - 0.5) * 2 * noise + drift * (i / 60)));
  }
  return out;
}

const r = mulberry32(42);

export const UCL = 900;   // 목업 관리 상한 (실값은 B 실력치 엔진 API)
export const CENTER = 700;

export const CHAMBER_SUMMARIES: ChamberSummary[] = [
  { chamberId: "SIM_CH_1", status: "NORMAL", statusNote: "정상 가동", c65Series: series(r, 695, 38), driftScore: 0.21, anomalyScore: 0.06, holdCount: 0, rfProgress: 0.62 },
  { chamberId: "SIM_CH_2", status: "PM_QUAL", statusNote: "PM 완료 — Qual 진행", c65Series: series(r, 710, 30), driftScore: 0.18, anomalyScore: 0.09, holdCount: 0, rfProgress: 0.03 },
  { chamberId: "SIM_CH_3", status: "WARNING", statusNote: "C11 N3 위반 — 점진 드리프트", c65Series: series(r, 705, 34, 175), driftScore: 0.74, anomalyScore: 0.13, holdCount: 3, rfProgress: 0.81 },
  { chamberId: "SIM_CH_4", status: "NORMAL", statusNote: "정상 가동", c65Series: series(r, 688, 36), driftScore: 0.16, anomalyScore: 0.05, holdCount: 0, rfProgress: 0.44 },
  { chamberId: "SIM_CH_5", status: "NORMAL", statusNote: "정상 가동", c65Series: series(r, 701, 33), driftScore: 0.24, anomalyScore: 0.07, holdCount: 0, rfProgress: 0.29 },
  { chamberId: "SIM_CH_6", status: "NORMAL", statusNote: "정상 가동", c65Series: series(r, 693, 35), driftScore: 0.19, anomalyScore: 0.08, holdCount: 0, rfProgress: 0.55 },
];

const SENSORS = ["C11", "C62", "C17", "C31", "C15", "C57"];
const RULES = ["N1", "N2", "N3", "N5", "N6"];

let waferSeq = 3815;
export function nextWaferRow(rand: () => number = Math.random): WaferRow {
  waferSeq += 1;
  const ch = CHAMBER_SUMMARIES[waferSeq % 6];
  const drifting = ch.status === "WARNING";
  const pred = Math.round(
    (drifting ? 860 : 700) + (rand() - 0.5) * 70 + (drifting ? rand() * 60 : 0),
  );
  const risk: WaferRow["risk"] = pred > UCL ? "high" : pred > 800 ? "mid" : "low";
  const s1 = SENSORS[Math.floor(rand() * SENSORS.length)];
  const s2 = SENSORS[(SENSORS.indexOf(s1) + 1) % SENSORS.length];
  const s3 = SENSORS[(SENSORS.indexOf(s1) + 3) % SENSORS.length];
  return {
    waferId: `C64_${waferSeq}_${ch.chamberId.replace("SIM_", "")}`,
    chamberId: ch.chamberId,
    predictedC65: pred,
    risk,
    shapTop3: [
      { sensor: drifting ? "C11" : s1, contribution: 0.42 + rand() * 0.2 },
      { sensor: s2, contribution: 0.18 + rand() * 0.12 },
      { sensor: s3, contribution: 0.08 + rand() * 0.08 },
    ],
    spcFlags: drifting && rand() > 0.4
      ? [{ sensor: "C11", rule: "N3" }]
      : rand() > 0.92
        ? [{ sensor: s1, rule: RULES[Math.floor(rand() * RULES.length)] }]
        : [],
    ts: new Date().toISOString(),
  };
}

export const INITIAL_STREAM: WaferRow[] = (() => {
  const rr = mulberry32(7);
  return Array.from({ length: 9 }, () => nextWaferRow(rr)).reverse();
})();

/* ============================================================================
   S1 v1.3 — 결정 대기 큐 · VaR · 포커스 챔버 주석 (목업)
   W4에서 GET /incidents?status=pending&sort=priority 로 교체
   ========================================================================== */

export const WAFER_UNIT_COST_USD = 18_500; // $15k~22k 중간값 (기획서 §비용)

export type IncidentOption = "recipe" | "limit" | "maintenance" | "escalate";

export interface PendingIncident {
  incidentId: string;          // INC-<YYYYMMDD>-<CHAMBER>-<SEQ> (헌법 6-4)
  chamberId: string;
  severity: number;            // 0~1
  holdCount: number;
  elapsedMin: number;          // 발생 후 경과(분) — 타임아웃 24h
  recommendation: string;      // Supervisor 추천 1줄
  option: IncidentOption;
  confidence: "high" | "mid" | "low";
  claimedBy: string | null;    // null=미지정
  lifecycle: "pending" | "reopened";
}

export const PENDING_INCIDENTS: PendingIncident[] = [
  {
    incidentId: "INC-20260711-CH_3-001",
    chamberId: "SIM_CH_3",
    severity: 0.82,
    holdCount: 3,
    elapsedMin: 47,
    recommendation: "공정 조건 이탈 — Recipe 튜닝안 (C4 gas flow -1.8%)",
    option: "recipe",
    confidence: "high",
    claimedBy: null,
    lifecycle: "pending",
  },
  {
    incidentId: "INC-20260710-CH_5-002",
    chamberId: "SIM_CH_5",
    severity: 0.55,
    holdCount: 0,
    elapsedMin: 1130,
    recommendation: "기준선 노후 — 실력치 재설정 (C31, +4.2%)",
    option: "limit",
    confidence: "mid",
    claimedBy: "김엔지",
    lifecycle: "reopened",
  },
];

/** 우선순위 = severity × (1 + HOLD 물량 가중) — 정의서 S3 정렬 기준과 동일 */
export function incidentPriority(i: PendingIncident): number {
  return i.severity * (1 + i.holdCount * 0.4);
}

export const OPTION_LABEL: Record<IncidentOption, string> = {
  recipe: "Recipe R2R",
  limit: "실력치",
  maintenance: "정비",
  escalate: "에스컬레이션",
};

/** 포커스 챔버 렌더 주석 — 센서 3점 (표시명 + C코드 병기, 헌법 6-4) */
export interface FocusSensor {
  num: 1 | 2 | 3;
  code: string;        // C코드 (판정 로직 기준)
  label: string;       // 표시명 (표시 계층 전용)
  value: string;
  unit: string;
  alarm: boolean;
  lo: string;          // 레인지 밴드 하한 라벨
  hi: string;          // 레인지 밴드 상한 라벨
  pos: number;         // 0~1 — 밴드 내 현재 위치
  spark: number[];     // 최근 추이 (표시용 합성)
}

function synthSeries(seed: number, n: number, base: number, noise: number, drift = 0): number[] {
  const rr = mulberry32(seed);
  return Array.from({ length: n }, (_, i) => base + (rr() - 0.5) * 2 * noise + drift * (i / n));
}

export function focusSensors(ch: ChamberSummary): FocusSensor[] {
  const warn = ch.status === "WARNING" || ch.status === "CRITICAL";
  return [
    {
      num: 1, code: "C11", label: "DC bias", value: warn ? "-71.4" : "-64.9", unit: "V", alarm: warn,
      lo: "-80", hi: "-55", pos: warn ? 0.30 : 0.58,
      spark: synthSeries(11, 24, -65, 1.6, warn ? -6.5 : 0),
    },
    {
      num: 2, code: "C4", label: "Gas flow", value: warn ? "418.2" : "402.6", unit: "sccm", alarm: false,
      lo: "380", hi: "430", pos: warn ? 0.76 : 0.45,
      spark: synthSeries(4, 24, 403, 3.5, warn ? 14 : 0),
    },
    {
      num: 3, code: "C17", label: "Temp", value: "61.4", unit: "°C", alarm: false,
      lo: "55", hi: "70", pos: 0.43,
      spark: synthSeries(17, 24, 61.4, 0.7),
    },
  ];
}
