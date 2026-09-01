// 알람 매트릭스 데이터 — 챔버축 × 알람종류축 (인트로 하단 스트립)
//
// ── 축에 올린 것과 안 올린 것 ────────────────────────────────────────────
// 매트릭스의 열이 되려면 **챔버 단위로 발동/미발동이 갈리는 채널**이어야 한다.
// 그래서 계약(§2·§3)의 지표 중 다음은 열로 만들지 않았다.
//   · `severity`     — 각 violation 의 속성이지 별개 채널이 아니다. **칸 색이 이미 severity 다.**
//                      열로 세우면 같은 정보를 두 번 그리게 된다.
//   · `shap_top3`    — 센서 기여도 **순위**(설명 채널). 발동/미발동이 없고 값이 센서 목록이라
//                      챔버×룰 격자에 담기지 않는다. 헌법 3-3 "SHAP 독립 채널 원칙"상
//                      룰 축과 섞지 않는 편이 계약에도 맞다.
//   · `context_score`— 알람 1건의 0~100 스칼라(Agent 가동 게이트 <31 미가동).
//                      룰이 아니라 **행 단위 요약**이라 열이 아니라 행 끝 숫자로 붙였다(CTX).
//
// 룰 카탈로그의 정본은 `src/agent_b_spc/nelson_rules.py` 의 RULES 테이블이다.
// id·창길이·severity 는 그 표를 그대로 옮긴 값이므로 여기서 임의로 바꾸지 않는다.

export type RuleSeverity = "CRITICAL" | "WARNING" | "INFO";

/** 열 묶음 — 발행 주체와 판정 근거가 다른 채널은 섞어 읽으면 안 된다 */
export type RuleGroup = "nelson" | "tttm" | "pred" | "rtd";

export const GROUPS: { id: RuleGroup; label: string; note: string }[] = [
  { id: "nelson", label: "NELSON (SPC)", note: "자기 챔버 시계열 — B 판정 (nelson_rules.py)" },
  { id: "tttm", label: "TTTM", note: "형제 챔버(fleet) 대비 — B 판정, 계약 §3 tttm" },
  { id: "pred", label: "예측·AE", note: "A 발행 (fdc.prediction) — SPC와 독립 채널" },
  { id: "rtd", label: "RTD", note: "연속 K 롤업 — 장비 정지 「제안」" },
];

/** KEEP* 분위수 그룹(헌법 1-1 예외 2)에서 이 룰이 어떻게 취급되는가 */
export type KeepStar = "active" | "quantile" | "excluded";

export interface RuleSpec {
  id: string;
  group: RuleGroup;
  /** 판정 창 길이(점) — nelson_rules.py RuleSpec 2번째 인자. 합성·비-Nelson 채널은 null */
  window: number | null;
  /** 이 채널이 낼 수 있는 **최고** 등급 (열 머리 밑줄 색) */
  severity: RuleSeverity;
  keepStar: KeepStar;
  /** σ-존 의존 — 가한계(PROV) Phase 1 광폭 관리선 구간에서 판정이 유예된다 */
  sigmaZone: boolean;
  desc: string;
}

export const RULES: RuleSpec[] = [
  // ── Nelson N1~N8 — 정본 nelson_rules.py RULES ──
  { id: "N1", group: "nelson", window: 1, severity: "CRITICAL", keepStar: "quantile", sigmaZone: true, desc: "1점이 3σ 밖" },
  { id: "N2", group: "nelson", window: 9, severity: "WARNING", keepStar: "active", sigmaZone: false, desc: "9점 연속 중심선 한쪽" },
  { id: "N3", group: "nelson", window: 6, severity: "WARNING", keepStar: "active", sigmaZone: false, desc: "6점 연속 증가 또는 감소" },
  { id: "N4", group: "nelson", window: 14, severity: "WARNING", keepStar: "active", sigmaZone: false, desc: "14점 연속 교대 진동" },
  { id: "N5", group: "nelson", window: 3, severity: "WARNING", keepStar: "excluded", sigmaZone: true, desc: "3점 중 2점이 2σ 밖" },
  { id: "N6", group: "nelson", window: 5, severity: "INFO", keepStar: "excluded", sigmaZone: true, desc: "5점 중 4점이 1σ 밖" },
  { id: "N7", group: "nelson", window: 15, severity: "INFO", keepStar: "excluded", sigmaZone: true, desc: "15점 연속 1σ 이내" },
  { id: "N8", group: "nelson", window: 8, severity: "WARNING", keepStar: "excluded", sigmaZone: true, desc: "8점 연속 1σ 밖" },

  // ── TTTM (계약 §3 `tttm`) — rule_id 가 없는 별도 오브젝트지만 챔버 단위 발동이라 축에 선다 ──
  { id: "GAP", group: "tttm", window: null, severity: "WARNING", keepStar: "active", sigmaZone: false, desc: "fleet median 대비 이탈 (tttm.score · warning/critical 2단)" },
  { id: "SUSP", group: "tttm", window: null, severity: "WARNING", keepStar: "active", sigmaZone: false, desc: "reference_suspect — 다수 챔버 공통이동, 참조/공통 원인 의심. C Supervisor STEP 0 에서 4지선다를 건너뛰고 escalate 로 단락" },

  // ── 예측·AE (계약 §2 fdc.prediction) — SPC 와 독립 채널 ──
  { id: "ANOM", group: "pred", window: null, severity: "CRITICAL", keepStar: "active", sigmaZone: false, desc: "anomaly_score ≥ 0.2 (AE ae_score · Qual 임계와 동일 눈금)" },
  { id: "DRIFT", group: "pred", window: null, severity: "WARNING", keepStar: "active", sigmaZone: false, desc: "drift_score ≥ 0.2 (per-chamber EWMA) — 재학습 트리거가 아니라 서빙 보호·에스컬레이션 (헌법 3-3)" },
  { id: "B9", group: "pred", window: null, severity: "CRITICAL", keepStar: "active", sigmaZone: false, desc: "crazy wafer 1장 격리 — predicted_c65 > P99 + anomaly 이중 확인. violations[] 합성 마커(계약 §4-B)" },

  // ── RTD ──
  { id: "RTD", group: "rtd", window: null, severity: "CRITICAL", keepStar: "active", sigmaZone: false, desc: "reference_suspect 연속 K 롤업 → 3단 장비 정지 「제안」" },
];

/** 칸 상태. na = 그 챔버에서 그 채널이 애초에 안 도는 칸 (판정 유예·룰셋 제외) */
export type CellState = "na" | "off" | "info" | "warn" | "crit";

export interface Cell {
  state: CellState;
  /** 진행 중(헤드까지 열린 위반) 여부 — 회복된 과거 위반은 false */
  open: boolean;
  note: string;
}

/**
 * 그 챔버에서 **판정 자체가 유예되는** 채널 — 매트릭스와 간트의 **단일 소스**.
 * (구 AlarmMatrix 지역 함수. 간트도 같은 판정을 그려야 해서 여기로 올렸다 —
 *  따로 적으면 "매트릭스는 빗금인데 간트는 무이상"으로 어긋난다.)
 *
 * PM 중 = 전 채널 판정 없음 (설비가 안 돌아 표본이 없다).
 * 가한계(PROV) Phase 1 = 광폭 관리선이라 σ-존 룰은 의미가 없고 추세 룰만 산다.
 *   TTTM 도 유예 — 가한계 챔버는 fleet median 산출에서 제외된다 (B6-3).
 *   RTD 는 reference_suspect 롤업이라 SUSP 가 유예되면 함께 선다.
 *   반대로 예측·AE(ANOM·DRIFT)는 관리선과 무관한 독립 채널이라 그대로 돈다.
 */
export function naRules(sev: string): Set<string> {
  if (sev === "pm") return new Set(RULES.map((r) => r.id));
  if (sev === "provisional") {
    return new Set([...RULES.filter((r) => r.sigmaZone).map((r) => r.id), "GAP", "SUSP", "RTD"]);
  }
  return new Set();
}

/**
 * 시간 모델 — **하루 단위**. 달력이 날짜를 고르고 간트가 그 하루를 그린다.
 * 단위는 자정부터의 분(0 = 00:00, 1440 = 24:00).
 */
export const DAY_MIN = 1440;
/** 목업 기준 '지금' — 오늘 19:00. Date.now() 를 안 쓰는 이유: 매 렌더 값이 달라지면
 *  구간이 미세하게 흔들리고 달력 집계도 깜빡인다. 실배선 시 서버 시각으로 교체. */
export const NOW_MIN = 1140;
/** 목업 기준일 (달력 초기 선택) */
export const TODAY_ISO = "2026-08-06";

/** 발동 구간 하나. to >= NOW_MIN 이고 그날이 오늘이면 아직 열려 있다(진행 중) */
export interface Band {
  from: number;
  to: number;
  state: Exclude<CellState, "na" | "off">;
  note: string;
  /** Nelson 룰이 어느 센서에서 터졌나 (C코드 — 헌법 6-4). 세부 보기가 SPC 차트를 이 센서로 돌린다 */
  sensor?: "C4" | "C11" | "C17";
}

/**
 * 목업 발동 이력 — **실배선 전 자리표시이자 이 화면들의 단일 소스**.
 *
 * 매트릭스(현재 상태)와 간트(시간 구간)가 **같은 데이터에서 파생**된다.
 * 둘을 따로 적어두면 반드시 어긋난다 — "매트릭스는 빨간데 간트엔 구간이 없다" 같은 식으로.
 * 실데이터가 붙어도 구조는 같다: 아래 출처들이 (챔버, 채널, 시작~끝) 형태로 오고,
 * 매트릭스는 그중 **마지막 구간**만 보는 뷰가 된다.
 *
 *   N1~N8·B9 → `/api/incidents` evidence 의 `violations[]` 를 (chamber_id, rule_id) 로 집계
 *   GAP·SUSP → `fdc.alert` 의 `tttm.score` · `tttm.reference_suspect`
 *   ANOM·DRIFT → `fdc.prediction` 의 `anomaly_score` · `drift_score` (임계 0.2)
 *   RTD → `/api/rtd/status` 의 `lane1.fired_chambers`
 */
export const MOCK_BANDS: Record<string, Record<string, Band[]>> = {
  CH1: {
    N7: [{ from: 420, to: NOW_MIN, state: "info", note: "15점 연속 1σ 이내 — 과안정", sensor: "C11" }],
  },
  CH2: {
    N5: [{ from: 920, to: NOW_MIN, state: "warn", note: "C11 3점 중 2점 2σ 밖", sensor: "C11" }],
    N6: [{ from: 540, to: 660, state: "info", note: "5점 중 4점 1σ 밖", sensor: "C17" }],
    DRIFT: [{ from: 480, to: 600, state: "warn", note: "drift 0.24 → 0.17" }],
  },
  CH3: {
    N1: [{ from: 1000, to: NOW_MIN, state: "crit", note: "관리선(±3σ) 밖", sensor: "C4" }],
    N2: [{ from: 830, to: NOW_MIN, state: "warn", note: "C4 9점 연속 편향", sensor: "C4" }],
    N3: [
      { from: 300, to: 375, state: "warn", note: "C4 6점 연속 상승 (1차)", sensor: "C4" },
      { from: 540, to: 690, state: "warn", note: "C4 6점 연속 상승 (재발)", sensor: "C4" },
    ],
    N8: [{ from: 270, to: 360, state: "warn", note: "C11 8점 연속 1σ 밖", sensor: "C11" }],
    GAP: [{ from: 760, to: NOW_MIN, state: "crit", note: "fleet median 대비 이탈" }],
    SUSP: [{ from: 870, to: NOW_MIN, state: "warn", note: "C17 공통이동 — 참조 의심 (escalate 단락)" }],
    ANOM: [{ from: 900, to: NOW_MIN, state: "crit", note: "anomaly 0.83 ≥ 0.2" }],
    DRIFT: [{ from: 600, to: NOW_MIN, state: "warn", note: "drift 0.31 ≥ 0.2" }],
    B9: [{ from: 970, to: 995, state: "crit", note: "crazy wafer 1장 격리 — 처분 대기" }],
  },
  CH4: {
    N3: [{ from: 450, to: 660, state: "warn", note: "6점 연속 상승 (추세 룰은 가한계에서도 유효)", sensor: "C4" }],
    ANOM: [{ from: 500, to: 580, state: "warn", note: "anomaly 0.22" }],
  },
};

/**
 * 점수형 채널의 세부 그래프 규격.
 * 임계는 **실제 config 값**이다 — TTTM 은 `params.yaml` `spc.tttm_warning/critical`(2.0/3.0),
 * AE 는 계약 §2 의 `0.2`(= `qual.pass_ae_max` · `ct.ae_anomaly_threshold` 동일 눈금).
 * 여기 숫자를 임의로 바꾸면 화면이 config 와 다른 말을 하게 된다.
 */
export const SCORE_META: Record<string, {
  field: string; warn: number; crit?: number; max: number; dec: number; desc: string;
}> = {
  ANOM: { field: "anomaly_score", warn: 0.2, max: 1, dec: 2, desc: "AE 재구성 오차를 ECDF 캘리브레이션한 값 [0,1]" },
  DRIFT: { field: "drift_score", warn: 0.2, max: 1, dec: 2, desc: "챔버별 EWMA 입력 드리프트 [0,1] — 서빙 보호·에스컬레이션" },
  GAP: { field: "tttm.score", warn: 2.0, crit: 3.0, max: 4, dec: 1, desc: "fleet median 대비 |gap σ|" },
};

/** 세부 그래프 표본 간격(분) — 15분 = 하루 96점 */
export const SCORE_STEP = 15;

/**
 * 점수 시계열 — **간트 구간과 정합**하게 만든다.
 * 구간 안에서는 임계를 넘고 밖에서는 안 넘는다. 이게 어긋나면 "간트는 빨간데 그래프는
 * 임계 아래"가 되어 화면이 스스로를 반박한다.
 */
export function scoreSeries(chamberId: string, iso: string, rule: string): { t: number; v: number }[] {
  const meta = SCORE_META[rule];
  if (!meta) return [];
  const bands = bandsForDay(chamberId, iso)[rule] ?? [];
  const end = iso === TODAY_ISO ? NOW_MIN : DAY_MIN;
  const out: { t: number; v: number }[] = [];

  for (let t = 0; t <= end; t += SCORE_STEP) {
    // 느린 성분(시간 단위) + 빠른 성분(표본 단위) — 톱니 대신 물결로 보이게
    const slow = rnd(`${iso}|${chamberId}|${rule}|h${Math.floor(t / 60)}`);
    const fast = rnd(`${iso}|${chamberId}|${rule}|t${t}`);
    const wob = slow * 0.7 + fast * 0.3;

    const b = bands.find((x) => t >= x.from && t <= x.to);
    let v: number;
    if (b) {
      // 구간 안 — 임계 위. crit 는 임계의 2배 근방까지
      const ceil = b.state === "crit" ? meta.warn * 2.6 : meta.warn * 1.7;
      v = meta.warn * 1.06 + wob * (ceil - meta.warn * 1.06);
    } else {
      v = meta.warn * (0.18 + wob * 0.68);            // 구간 밖 — 임계 아래
    }
    out.push({ t, v: Math.min(meta.max, v) });
  }
  return out;
}

const SEV_RANK: Record<string, number> = { info: 1, warn: 2, crit: 3 };

// ── 과거 일자 목업 ────────────────────────────────────────────────────────
// 달력은 한 달치가 필요한데 손으로 31일 × 4챔버를 적을 수는 없다. **결정적 해시**로 만든다
// (Math.random 금지 — 렌더마다 값이 바뀌면 달력이 깜빡이고 간트와도 어긋난다).
// 중요한 건 달력과 간트가 **같은 함수에서 나온다**는 것이다: 달력이 "심각"이라 칠한 날은
// 간트에도 반드시 crit 구간이 있다.

/** 문자열 → 0..1 결정적 난수 (FNV-1a 변형) */
function rnd(seed: string): number {
  let h = 2166136261;
  for (let i = 0; i < seed.length; i++) {
    h ^= seed.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 100000) / 100000;
}

/** 그날 그 챔버에서 돌 수 있는 채널 후보 (판정 유예 채널은 챔버 상태로 따로 걸러진다) */
const SYNTH_POOL = ["N1", "N2", "N3", "N5", "N6", "N8", "GAP", "SUSP", "ANOM", "DRIFT"];

/** 과거 하루치 합성 — 대부분의 날은 조용하고, 가끔 한두 채널이 뜬다 */
function synthDay(chamberId: string, iso: string): Record<string, Band[]> {
  const out: Record<string, Band[]> = {};
  const quiet = rnd(`${iso}|${chamberId}|q`);
  if (quiet < 0.55) return out;                       // 절반 이상은 무이상
  const n = quiet < 0.85 ? 1 : 2;
  for (let k = 0; k < n; k++) {
    const pick = SYNTH_POOL[Math.floor(rnd(`${iso}|${chamberId}|p${k}`) * SYNTH_POOL.length)];
    if (out[pick]) continue;
    const from = Math.floor(rnd(`${iso}|${chamberId}|f${k}`) * (DAY_MIN - 240)) + 60;
    const dur = Math.floor(rnd(`${iso}|${chamberId}|d${k}`) * 300) + 45;
    const sev = rnd(`${iso}|${chamberId}|s${k}`);
    out[pick] = [{
      from,
      to: Math.min(DAY_MIN, from + dur),
      state: sev > 0.86 ? "crit" : sev > 0.35 ? "warn" : "info",
      note: `${pick} 발동 (과거 이력)`,
    }];
  }
  return out;
}

/** 그 챔버·그 날짜의 발동 구간 — 달력과 간트의 **단일 소스** */
export function bandsForDay(chamberId: string, iso: string): Record<string, Band[]> {
  if (iso === TODAY_ISO) return MOCK_BANDS[chamberId] ?? {};
  if (iso > TODAY_ISO) return {};                     // 미래는 비운다
  return synthDay(chamberId, iso);
}

/**
 * 그날 가장 심한 (챔버, 채널) — **세부 보기의 기본 선택**.
 * 착지했을 때 세부 카드가 비어 있으면 "클릭하면 열린다"를 알 방법이 없다.
 * 처음부터 하나를 펴 두면 상호작용이 '발견'이 아니라 '전환'이 된다.
 * 진행 중인 것을 종료된 것보다 우선한다.
 */
export function worstPickForDay(
  chamberIds: string[], iso: string,
): { chamber: string; rule: string; sensor?: Band["sensor"] } | null {
  let best: { chamber: string; rule: string; sensor?: Band["sensor"]; score: number } | null = null;
  for (const ch of chamberIds) {
    for (const [rule, bands] of Object.entries(bandsForDay(ch, iso))) {
      for (const b of bands) {
        const open = iso === TODAY_ISO && b.to >= NOW_MIN;
        const score = SEV_RANK[b.state] * 10 + (open ? 5 : 0) + (b.to - b.from) / 1000;
        if (!best || score > best.score) best = { chamber: ch, rule, sensor: b.sensor, score };
      }
    }
  }
  return best ? { chamber: best.chamber, rule: best.rule, sensor: best.sensor } : null;
}

export interface DayStat {
  worst: "off" | "info" | "warn" | "crit";
  crit: number;
  warn: number;
  info: number;
  total: number;
}

/** 하루를 전 챔버에 걸쳐 집계 — 달력 칸 한 개 */
export function dayStat(iso: string, chamberIds: string[]): DayStat {
  const s: DayStat = { worst: "off", crit: 0, warn: 0, info: 0, total: 0 };
  for (const ch of chamberIds) {
    for (const bands of Object.values(bandsForDay(ch, iso))) {
      for (const b of bands) {
        s[b.state] += 1;
        s.total += 1;
        if (SEV_RANK[b.state] > (SEV_RANK[s.worst] ?? 0)) s.worst = b.state;
      }
    }
  }
  return s;
}

export interface SpanStat {
  /** 그 구간에서 **최고 등급이 심각이었던 날**의 수 */
  critDays: number;
  warnDays: number;
  /** 발동이 있는 날이 끊기지 않고 이어진 최대 일수 — 만성인지 일회성인지를 가른다 */
  longest: number;
}

const pad2 = (n: number): string => String(n).padStart(2, "0");

/**
 * 날짜 목록을 **일수 셋**으로 접는다 — 이 파일의 기간 집계 **단일 구현**.
 *
 * 세는 단위가 건수가 아니라 날인 이유: 이 값들이 답할 질문이 "언제부터"라서다.
 * 같은 기간에 3건이 하루에 몰린 것과 3일에 걸친 것은 전혀 다른 이야기인데 건수로는 같다.
 *
 * 구간을 **목록으로 받는** 이유: 부르는 쪽마다 창이 다르기 때문이다 (달력 = 그 달 /
 * 종합 현황 = 최근 N일). 창은 달라도 세는 법은 하나여야 한다 — 두 화면이 서로 다른
 * 숫자를 말하는 건 괜찮지만, **다르게 세는** 건 안 된다.
 */
function spanStat(isoList: string[], chamberIds: string[]): SpanStat {
  const s: SpanStat = { critDays: 0, warnDays: 0, longest: 0 };
  let run = 0;
  for (const iso of isoList) {
    const st = dayStat(iso, chamberIds);
    if (st.worst === "crit") s.critDays += 1;
    else if (st.worst === "warn") s.warnDays += 1;
    // 연속은 **등급과 무관하게 발동이 있었는가**로 센다 (INFO 도 끊기지 않은 날이다)
    if (st.total > 0) { run += 1; s.longest = Math.max(s.longest, run); } else run = 0;
  }
  return s;
}

/*
 * 구 `monthStat(y, m, chamberIds)` — 한 달을 접던 함수. **삭제** (2026-08-11).
 * 달력 발치 요약이 마지막 사용처였는데 그 창도 최근 N일로 바뀌면서 부르는 곳이 없어졌다.
 * 월별 집계가 답하는 질문이 이 앱에 없다 — 월초엔 표본이 며칠뿐이고(8/6 기준 엿새치),
 * 리포팅 단위로 월 경계를 쓰는 화면도 없다(KPI 7일·일별 / 알람 이력 30일).
 * 되살릴 일이 생기면 `spanStat` 에 그 달의 날짜 목록을 넘기면 된다 — 세는 법은 그대로다.
 */

/** 알람 이력 요약의 창 — **최근 N일**. 창 길이를 바꾸면 두 화면의 표기가 함께 따라간다 */
export const RECENT_DAYS = 30;

/**
 * **최근 N일**(끝날 포함)을 접는다 — 종합 현황 「알람 이력」.
 *
 * 왜 달(月)이 아니라 굴러가는 창인가 (2026-08-11 개정):
 *   ① **달이 바뀌면 0 으로 리셋된다.** 1일에 열면 "심각 0일"인데, 그건 조용해서가 아니라
 *      창이 하루치라서다. 어제까지 사흘 연속 심각이었어도 화면은 아무 말도 안 한다.
 *   ② **분모가 매일 달라진다.** 6일에 보는 "이번 달"은 6일치, 28일에 보는 건 28일치다.
 *      같은 카드의 숫자가 날짜에 따라 다른 크기의 창을 말하니 어제와 오늘을 비교할 수 없다 —
 *      이 화면이 0 인 칸도 지우지 않는 이유("자리가 고정돼야 비교된다")와 정면으로 어긋난다.
 *   ③ **최장 연속이 월 경계에서 잘린다.** 7/30~8/2 나흘 연속이면 달 기준으론 "2일"이다.
 * 창을 고정하면 셋 다 사라진다: 언제 열어도 30일치이고, 경계가 없다.
 *
 * @param endIso 창의 **끝날**(포함). 목업 세계의 '오늘'(`TODAY_ISO`)을 넣는다 — 헤더 시계의
 *   실제 벽시계와는 다른 값이다(`ui/Clock.tsx` 참조).
 */
export function recentStat(endIso: string, days: number, chamberIds: string[]): SpanStat {
  const [y, m, d] = endIso.split("-").map(Number);
  const list: string[] = [];
  // ⚠ `new Date(endIso)` 로 파싱하지 않는다 — ISO 날짜 문자열은 **UTC 자정**으로 읽혀,
  //   음수 오프셋 지역에서는 `getDate()` 가 하루 앞당겨진다. 성분으로 받아 로컬로 짓는다.
  //   `d - i` 가 음수여도 Date 가 달·해 경계를 알아서 넘긴다 (창이 월을 가로지르는 게 요점).
  for (let i = days - 1; i >= 0; i--) {
    const t = new Date(y, m - 1, d - i);
    list.push(`${t.getFullYear()}-${pad2(t.getMonth() + 1)}-${pad2(t.getDate())}`);
  }
  return spanStat(list, chamberIds);
}

/**
 * 구간 목록 → 매트릭스 칸 하나.
 * 색은 창 안에서 **가장 심한 등급**, 채움 여부는 **아직 열려 있는가**.
 * (가장 최근 구간만 보면 "심각했다가 잠깐 주의로 내려온" 상태가 주의로 보인다.)
 */
export function cellFromBands(bands: Band[] | undefined): Cell | null {
  if (!bands || bands.length === 0) return null;
  const worst = bands.reduce((a, b) => (SEV_RANK[b.state] > SEV_RANK[a.state] ? b : a));
  const openBand = bands.find((b) => b.to >= NOW_MIN);
  return {
    state: worst.state,
    open: openBand != null,
    note: `${(openBand ?? worst).note} · ${openBand ? "진행 중" : "회복"}`,
  };
}

const OFF_CELL: Cell = { state: "off", open: false, note: "무이상" };

/**
 * 챔버 한 대의 **칸 14개** — 매트릭스 격자 한 행이자, 종합 현황 타일의 "문제 몇 건"이다.
 *
 * 구 AlarmMatrix 지역 함수였는데 타일도 같은 수를 세야 해서 여기로 올렸다 (2026-08-08).
 * 따로 세면 **매트릭스에는 빨간 칸이 셋인데 타일은 "심각 2"** 같은 어긋남이 난다 —
 * 같은 화면에서 두 칸이 서로를 반박하는 셈이라, 세는 자리를 하나로 둔다.
 * (`naRules` 를 여기서 같이 적용하는 이유도 같다: 판정 유예 칸을 "무이상"으로 세면 안 된다.)
 */
export function cellsForChamber(chamberId: string, sev: string): Record<string, Cell> {
  const na = naRules(sev);
  const bands = MOCK_BANDS[chamberId] ?? {};
  const out: Record<string, Cell> = {};
  for (const r of RULES) {
    if (na.has(r.id)) { out[r.id] = { state: "na", open: false, note: "판정 유예" }; continue; }
    out[r.id] = cellFromBands(bands[r.id]) ?? OFF_CELL;
  }
  return out;
}

/** 칸 14개 → 등급별 개수. 타일 한 줄("심각 4 · 주의 3")의 재료 */
export interface CellCount { crit: number; warn: number; info: number; na: number }
export function countCells(cells: Record<string, Cell>): CellCount {
  const c: CellCount = { crit: 0, warn: 0, info: 0, na: 0 };
  for (const k of Object.keys(cells)) {
    const st = cells[k].state;
    if (st === "crit") c.crit++;
    else if (st === "warn") c.warn++;
    else if (st === "info") c.info++;
    else if (st === "na") c.na++;
  }
  return c;
}

/**
 * Context Score (계약 §3 `context_score`, 0~100) — 열이 아니라 **행 끝 숫자**.
 * 게이트 3중화: **≥31 자동 가동** / critical 즉시 / 수동. 31 미만은 Agent 가 안 돈다.
 */
export const CTX_GATE = 31;
export const MOCK_CTX: Record<string, number> = { CH1: 12, CH2: 44, CH3: 78, CH4: 27 };
