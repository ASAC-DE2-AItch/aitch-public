// 라이브 시뮬레이션 레이어 — 베이스(mock/fdc) 위에 스트림을 얹는다.
// SPC 곡선 = append-only: 자기 웨이퍼가 도착할 때만 오른쪽 끝에 1점 추가, 왼쪽 1점 탈락.
// 과거 점은 동결(불변). 플릿 스트림(펄스)과 차트는 같은 웨이퍼를 공유한다.
//
// 센서 구조: 챔버당 3개 센서(C4·C11·C17) 시리즈를 병렬 유지 — 한 wafer = 센서 3개 측정 동시 기록.
// S1 차트는 "주시 센서"(spcLabel의 센서 — 알람 유발/워스트 기여) 기본, 칩으로 전환 가능.
//
// σ 캘리브레이션: 차트 y좌표 ↔ σ는 단일 매핑(yOf/sigOf)으로 왕복하며,
// 헤더 큰 숫자·칩·LIVE 툴팁·TODAY Δ·Nelson 밴드는 전부 "곡선"에서 파생된다 (숫자 따로 그림 따로 금지).
//
// 실배선 계층 (W6-①):
//   · 예측(P5-4b): 초기 이력 = REST(/api/predictions/recent), append = SSE(/api/predictions/stream)
//   · 센서 곡선(W6-①): 초기 이력 = REST(/api/raw/recent), append = SSE(/api/raw/stream) —
//     게이트웨이 raw_feed가 wafer당 settled 평균을 현행 관리선으로 σ 환산해 공급.
//     sig가 없는 센서(관리선 미시딩 — 예: C4)는 그 센서 곡선만 목업 유지 (실값 위장 금지).
//   · F2·F3: /api/limits/corrections 폴링 — APPLIED 신규 건을 승인 마커로, limit_version을 칩으로.
// 실패/피드 빈 상태 = 목업 유지 (시연 안전망 — 기존 동작 그대로).
import { useEffect, useMemo, useRef, useState } from "react";
import {
  chambers as baseChambers, incidents as baseIncidents,
  initialPulse, pulseNextNo, fmtAge,
  type Chamber, type Incident, type PulseItem,
} from "../mock/fdc";

/** 결정적 의사난수 (seed, n 기반 — 리렌더 간 일관) */
const prand = (seed: number, n: number): number => {
  const x = Math.sin(seed * 127.1 + n * 311.7) * 43758.5453;
  return x - Math.floor(x);
};
const jit = (seed: number, n: number, amp: number): number =>
  (prand(seed, n) - 0.5) * 2 * amp;

const N_POINTS = 40;            // 곡선 점 개수 = 최근 40 wafer (S1은 최근 창, 장기 이력은 S2)
/** 마지막 점(LIVE) x — 데이터의 끝. 축·UCL선·진행 중 밴드 전부 여기서 끝난다 (우측은 툴팁 여백) */
export const X_MAX = 490;
const CHAMBER_WAFER_SEC = 12;   // 챔버 1대의 wafer 처리 시간 (데모 스케일)
export const xGrid = Array.from({ length: N_POINTS }, (_, i) =>
  Math.round((i * X_MAX) / (N_POINTS - 1)));

// σ ↔ y 단일 매핑 — W6-① 실데이터 대응: 표시창 = ±3.8σ 대칭 (0σ 중앙 y101, 1σ≈19.7px).
// 구 매핑(−0.1~+3.8σ, 1σ=38px)은 목업 상승 서사 전용이라 실σ(음수·대편차)가 전부
// 상/하단 클램프로 평평한 선이 됐다 (2026-07-27 실측: C11 −1.8σ 바닥 핀, C17 +10.7σ 천장 핀).
// 창 밖 값은 여전히 핀되지만 헤드 숫자·툴팁은 실값 그대로 (숨기지 않음 — 예: C17 시딩 재검 전).
export const SIG_MAX = 3.8;                        // 표시창 반폭 (±)
const PX_PER_SIG = 150 / (SIG_MAX * 2);            // 유효영역 y 26~176 = 150px
export const yOf = (sig: number): number => 101 - sig * PX_PER_SIG;
export const sigOf = (y: number): number => (101 - y) / PX_PER_SIG;
const clampY = (y: number): number => Math.min(176, Math.max(26, y));

/** S1 차트 대상 센서 (도면 콜아웃 3종) */
export const SENSOR_KEYS = ["C4", "C11", "C17"] as const;
export type SensorKey = (typeof SENSOR_KEYS)[number];
const SENSOR_LABEL: Record<SensorKey, string> = {
  C4: "C4 GAS FLOW", C11: "C11 DC BIAS", C17: "C17 TEMP",
};

/** σ ↔ 공학단위 환산 — 중심 = 도면 센서 기준치(ch.sensors), 1σ 폭 = 센서 종별 상수 */
const SENSOR_UNIT: Record<SensorKey, { sig: number; unit: string; dec: number }> = {
  C4: { sig: 6, unit: "sccm", dec: 1 },
  C11: { sig: 2.2, unit: "V", dec: 1 },
  C17: { sig: 0.8, unit: "°C", dec: 1 },
};
const SENSOR_CENTER: Record<SensorKey, "c4" | "c11" | "c17"> = { C4: "c4", C11: "c11", C17: "c17" };

/** σ값 → 공학단위 문자열 (예: sig=3 → UCL 실측 한계) */
function engOf(ch: Chamber, sensor: SensorKey, sig: number): string | null {
  if (ch.sensors == null) return null;
  const u = SENSOR_UNIT[sensor];
  const center = ch.sensors[SENSOR_CENTER[sensor]];
  return `${(center + sig * u.sig).toFixed(u.dec)} ${u.unit}`;
}

/** 주시 센서 = spcLabel의 첫 토큰 (알람 유발/워스트 기여 — 실배선 시 fdc.alert에서 자동 선정) */
export const watchOf = (ch: Chamber): SensorKey =>
  (ch.spcLabel.split(" ")[0] as SensorKey);

/** (챔버, 센서)의 σ 레벨 (y좌표) — 주시 센서만 챔버 레벨, 나머지는 저준위 정상 */
function levelFor(ch: Chamber, ci: number, si: number): number {
  const isWatch = SENSOR_KEYS[si] === watchOf(ch);
  if (isWatch) return ch.sev === "critical" ? yOf(3.1) : yOf(ch.drift ?? 0);
  return yOf(0.3 + prand(ci * 7 + si * 3 + 1, 0) * 0.35); // 0.3~0.65σ 정상 대역
}

/** 초기 이력 — critical 주시 센서는 상승 스토리, 그 외/PM은 자기 레벨 주변 동결 노이즈 */
function initSeries(ch: Chamber, ci: number, si: number): number[] {
  const level = levelFor(ch, ci, si);
  const seed = ci * 13 + si * 5 + 3;
  return Array.from({ length: N_POINTS }, (_, i) => {
    if (ch.sev === "critical" && SENSOR_KEYS[si] === watchOf(ch)) {
      const t = i / (N_POINTS - 1);
      return clampY(yOf(1.0) + (level - yOf(1.0)) * t + jit(seed, i, 5));
    }
    return clampY(level + jit(seed, i, SENSOR_KEYS[si] === watchOf(ch) ? 6 : 5));
  });
}

/** 신규 점 — wafer 순번으로 결정, 자기 레벨 주변 진동 */
function genY(ch: Chamber, ci: number, si: number, waferNo: number): number {
  const hot = ch.sev === "critical" && SENSOR_KEYS[si] === watchOf(ch);
  const amp = hot ? 8 : SENSOR_KEYS[si] === watchOf(ch) ? 7 : 5;
  return clampY(levelFor(ch, ci, si) + jit(ci * 41 + si * 17 + 7, waferNo, amp));
}

export interface SensorChart {
  sensor: SensorKey;
  /** 이 센서가 주시(알람 유발) 센서인가 */
  isWatch: boolean;
  points: [number, number][];
  /** UCL 위반 연속 구간 전체 — 회복해도 과거 위반은 기록 유지. open=헤드까지 진행 중 */
  nelsonBands: { x1: number; x2: number; open: boolean }[];
  headSig: number | null;
  /** 헤드 실측값 (공학 단위 — LIVE 툴팁용) */
  headEng: string | null;
  /** UCL(+3σ)의 공학 단위 임계치 — 기준선 라벨용 */
  uclEng: string | null;
  /** 헤더 아래 델타 라벨 — (헤드 σ − 좌측 끝 σ) 라이브 산출 */
  delta: string;
  /** W6-① — 실피드 곡선 여부 (raw_feed σ) */
  real?: boolean;
  /** F3 — 이 센서의 현행 관리선 버전 (실피드일 때만, 예: "v7") */
  limitVer?: string | null;
  /** F2 — 승인(관리선 갱신) 마커: 승인 시점 wafer의 차트 x + 라벨("v6→v7") */
  markers?: { x: number; label: string }[];
}

export interface LiveChamberView {
  ch: Chamber;
  /** 칩 표기 (수치는 주시 센서 헤드 파생, PROV/PM은 라벨) */
  sigChip: string;
  drift: string;        // "3.42" | "—"  (주시 센서 헤드 σ, 2자리)
  driftSigma: string;   // "3.4"  (주시 센서 헤드 σ, 1자리)
  c65: string;          // "1,284" | "—"
  sensors: { c11: string; c32: string; c17: string; c4: string };
  /** 센서 3종 차트 (C4·C11·C17) — SpcFlow가 선택 렌더 */
  charts: SensorChart[];
  /** fleet median 대비 주시 센서 헤드 σ 편차 (TTTM 관점) — PM은 "—" */
  fleetDelta: string;
  /** Nelson 위반 라벨 + 지속시간 (라이브) */
  nelsonLabel: string | null;
}

export interface LiveState {
  views: LiveChamberView[];
  incidents: (Incident & { age: string; ageM: number })[];
  pulse: (PulseItem & { fresh: boolean })[];
  /** 마지막 실 이벤트로부터의 실제 경과. **실피드가 한 번도 안 붙었으면 null** —
   *  숫자를 지어내지 않는다 (2026-08-03, 구 `tick % 4` 카운터 제거). */
  lag: string | null;
  worst: string;
}

/** SIM_CH_3 → CH3 (실피드 chamber_id → 목업 챔버 키) */
const chOf = (chamberId: unknown): string =>
  String(chamberId ?? "").replace(/^SIM_?CH_?/, "CH");

/** raw 피드 σ 시리즈 타입 — 챔버 → 센서 → 길이 N_POINTS σ 배열 */
type RawSeries = Record<string, Partial<Record<SensorKey, number[]>>>;
type RawMeta = Record<string, Partial<Record<SensorKey, { eng: number; ver: string | null }>>>;
type LimitMarker = { atCount: number; sensor: string; label: string };

export function useLiveFdc(): LiveState {
  const [tick, setTick] = useState(0);           // 2s — 연속 계측(센서 트레이스)만
  const [waferTick, setWaferTick] = useState(0); // 플릿 wafer 클럭 — wafer 단위 수치(σ·C65) 갱신
  const [pulse, setPulse] = useState<PulseItem[]>(initialPulse);
  const liveRef = useRef(false);                 // P5-4b — 실피드 연결 시 목업 wafer 클럭 정지
  const rawLiveRef = useRef(false);              // W6-① — raw 곡선 실피드 연결 시 목업 시리즈 정지
  // 챔버별 최신 wafer C65 — 시트 표시와 스트림의 단일 소스 (초기값 = 스트림 초기 항목)
  const [lastC65, setLastC65] = useState<Record<string, number>>(
    () => Object.fromEntries(initialPulse.map((p) => [p.ch, p.c65])),
  );
  const [ageBump, setAgeBump] = useState(0);
  // [챔버][센서] y 시리즈 — 초기 이력에서 시작, 이후 append-only (목업 형상 — raw 미가용 센서 폴백)
  const [series, setSeries] = useState<number[][][]>(
    () => baseChambers.map((ch, ci) => SENSOR_KEYS.map((_, si) => initSeries(ch, ci, si))),
  );
  // W6-① — raw 실곡선 (σ 값 그대로 유지, 렌더 시 yOf 변환)
  const [rawSeries, setRawSeries] = useState<RawSeries>({});
  const [rawMeta, setRawMeta] = useState<RawMeta>({});
  const [limitMarkers, setLimitMarkers] = useState<Record<string, LimitMarker[]>>({});
  const appendCntRef = useRef<Record<string, number>>({});   // 챔버별 raw append 수 (마커 x 계산)
  const waferNo = useRef(pulseNextNo);
  const freshRef = useRef(false);
  // 마지막 **실** 이벤트 수신 시각(ms). null = 실피드 미연결. LAG 배지의 유일한 근거다.
  const lastLiveMsRef = useRef<number | null>(null);

  // 센서 지터 — 2s (측정 진행 중인 현재값만 흔들림)
  useEffect(() => {
    const t = setInterval(() => setTick((v) => v + 1), 2000);
    return () => clearInterval(t);
  }, []);

  // P5-4b (2026-07-24) — 실피드 배선: 초기 이력 = REST(/api/predictions/recent),
  // append = SSE(/api/predictions/stream). 성공 시 목업 wafer 클럭 정지·실 wafer가 클럭.
  // 실패/피드 빈 상태 = 목업 유지 (시연 안전망 — 기존 동작 그대로).
  // W6-① 이후: 센서 곡선은 raw 피드가 담당 — raw 라이브면 여기선 시리즈를 건드리지 않는다.
  // 2026-07-31: 판정을 1회에서 **5초 재시도**로 — 게이트웨이보다 페이지가 먼저 뜨거나
  // 재시작 직후(버퍼 빈 시점)에 열리면 목업에 영구 고착돼 새로고침 전까지 안 붙던 실측 수정.
  useEffect(() => {
    let es: EventSource | null = null;
    let cancelled = false;
    const toPulse = (d: any): PulseItem => ({
      wafer: String(d.wafer_id ?? "—"),
      c65: Math.round(Number(d.predicted_c65 ?? 0)),
      warn: Number(d.predicted_c65 ?? 0) >= 1404,   // 실데이터 C65 P95
      ch: chOf(d.chamber_id),
      lot: String(d.lot_id ?? "L—"),
    });
    const applyWafer = (d: any) => {
      const ch = chOf(d.chamber_id);
      const n = waferNo.current++;
      freshRef.current = true;
      lastLiveMsRef.current = Date.now();
      setPulse((prev) => [toPulse(d), ...prev.slice(0, 7)]);
      setLastC65((m) => ({ ...m, [ch]: Math.round(Number(d.predicted_c65 ?? 0)) }));
      if (!rawLiveRef.current) {                     // raw 라이브면 실곡선이 시리즈 담당
        setSeries((prev) => prev.map((bySensor, ci) => {
          const c = baseChambers[ci];
          if (c.id !== ch || c.drift == null) return bySensor;
          return bySensor.map((ys, si) => [...ys.slice(1), genY(c, ci, si, n)]);
        }));
      }
      setWaferTick((v) => v + 1);
    };
    const init = async () => {
      if (es != null || cancelled) return;               // 이미 라이브 — 재진입 금지
      try {
        const r = await fetch("/api/predictions/recent?n=8", { signal: AbortSignal.timeout(2500) });
        if (!r.ok || cancelled) return;
        const b = await r.json();
        const items: any[] = Array.isArray(b?.items) ? b.items : [];
        if (!items.length) return;                       // 피드 비면 목업 유지 (다음 주기 재시도)
        liveRef.current = true;
        setPulse(items.slice(0, 8).map(toPulse));        // recent = 최신 우선 — 그대로 시딩
        setLastC65((m) => {
          const next = { ...m };
          for (const d of [...items].reverse()) next[chOf(d.chamber_id)] = Math.round(Number(d.predicted_c65 ?? 0));
          return next;
        });
        es = new EventSource("/api/predictions/stream");
        es.addEventListener("prediction", (ev) => {
          try { applyWafer(JSON.parse((ev as MessageEvent).data)); } catch { /* 스킵 */ }
        });
      } catch { /* 게이트 오프라인 — 목업 유지 (다음 주기 재시도) */ }
    };
    init();
    const retry = window.setInterval(init, 5000);        // 라이브 붙으면 init 재진입 가드가 무시
    return () => { cancelled = true; clearInterval(retry); es?.close(); };
  }, []);

  // W6-① — 센서 실곡선 배선: 초기 이력 = REST(/api/raw/recent), append = SSE(/api/raw/stream).
  // raw_feed가 wafer당 settled 평균을 현행 관리선으로 σ 환산해 준다 (F3의 σ-공간 문법:
  // 관리선 재설정 후 다음 wafer부터 새 버전 기준으로 정규화 — limit_version이 칩에 반영).
  // sig 없는 센서(예: C4 — 관리선 미시딩)는 그 센서만 목업 유지.
  useEffect(() => {
    let es: EventSource | null = null;
    let cancelled = false;
    const applyRaw = (d: any) => {
      const ch = chOf(d.chamber_id);
      appendCntRef.current[ch] = (appendCntRef.current[ch] ?? 0) + 1;
      setRawSeries((prev) => {
        const cur = { ...(prev[ch] ?? {}) } as Partial<Record<SensorKey, number[]>>;
        for (const k of SENSOR_KEYS) {
          const s = d.sensors?.[k]?.sig;
          if (s == null) continue;
          const arr = cur[k] ?? Array(N_POINTS).fill(Number(s));
          cur[k] = [...arr.slice(1), Number(s)];
        }
        return { ...prev, [ch]: cur };
      });
      setRawMeta((prev) => {
        const cur = { ...(prev[ch] ?? {}) } as RawMeta[string];
        for (const k of SENSOR_KEYS) {
          const e = d.sensors?.[k];
          if (e && e.sig != null) cur[k] = { eng: Number(e.eng), ver: e.limit_version ?? null };
        }
        return { ...prev, [ch]: cur };
      });
      lastLiveMsRef.current = Date.now();      // raw 실피드 수신 = LAG 근거 갱신
      setWaferTick((v) => v + 1);
    };
    const init = async () => {
      if (es != null || cancelled) return;               // 이미 라이브 — 재진입 금지
      try {
        const r = await fetch("/api/raw/recent?n=200", { signal: AbortSignal.timeout(2500) });
        if (!r.ok || cancelled) return;
        const b = await r.json();
        const items: any[] = Array.isArray(b?.items) ? b.items : [];
        if (!items.length) return;                       // raw 없으면 목업 유지 (다음 주기 재시도)
        rawLiveRef.current = true;
        liveRef.current = true;                          // 목업 wafer 클럭도 정지 (실 wafer가 클럭)
        // recent = 최신 우선 → 챔버별 시간순으로 재배열해 시딩
        const byCh: Record<string, any[]> = {};
        for (const it of [...items].reverse()) (byCh[chOf(it.chamber_id)] ??= []).push(it);
        const sInit: RawSeries = {};
        const mInit: RawMeta = {};
        for (const [ch, arr] of Object.entries(byCh)) {
          const last = arr.slice(-N_POINTS);
          for (const k of SENSOR_KEYS) {
            const vals = last.map((it) => {
              const s = it.sensors?.[k]?.sig;
              return s == null ? null : Number(s);
            });
            if (!vals.some((v) => v != null)) continue;   // 이 센서는 목업 유지 (예: C4)
            const first = vals.find((v) => v != null) as number;
            let prev = first;
            const filled = vals.map((v) => { if (v == null) return prev; prev = v; return v; });
            const full = Array(Math.max(0, N_POINTS - filled.length)).fill(first).concat(filled);
            (sInit[ch] ??= {})[k] = full;
            for (let i = last.length - 1; i >= 0; i--) {
              const e = last[i].sensors?.[k];
              if (e && e.sig != null) { (mInit[ch] ??= {})[k] = { eng: Number(e.eng), ver: e.limit_version ?? null }; break; }
            }
          }
          appendCntRef.current[ch] = appendCntRef.current[ch] ?? 0;
        }
        setRawSeries(sInit);
        setRawMeta(mInit);
        es = new EventSource("/api/raw/stream");
        es.addEventListener("wafer", (ev) => {
          try { applyRaw(JSON.parse((ev as MessageEvent).data)); } catch { /* 스킵 */ }
        });
      } catch { /* 게이트 오프라인 — 목업 유지 (다음 주기 재시도) */ }
    };
    init();
    const retry = window.setInterval(init, 5000);        // 라이브 붙으면 init 재진입 가드가 무시
    return () => { cancelled = true; clearInterval(retry); es?.close(); };
  }, []);

  // F2 — 승인(관리선 갱신) 마커: /api/limits/corrections 5초 폴링.
  // 초회 응답은 과거 이력이라 마커를 만들지 않고(프라임), 이후 "새로 나타난 APPLIED"만
  // 현재 헤드 wafer 위치에 마커로 박는다 — 승인 순간이 차트에 남는다.
  useEffect(() => {
    const seen = new Set<string>();
    let primed = false;
    let stopped = false;
    const poll = async () => {
      try {
        const r = await fetch("/api/limits/corrections?n=10", { signal: AbortSignal.timeout(2500) });
        if (!r.ok || stopped) return;
        const b = await r.json();
        const rows: any[] = Array.isArray(b?.corrections) ? b.corrections : [];
        for (const c of rows) {
          const id = String(c.correction_id ?? "");
          if (!id || seen.has(id)) continue;
          if (!String(c.status ?? "").includes("APPLIED")) continue;
          seen.add(id);
          if (!primed) continue;                     // 과거 이력은 마커 생성 안 함
          const ch = chOf(c.chamber_id);
          const label = `${c.limit_version_before ?? "?"}→${c.limit_version_after ?? "?"}`;
          setLimitMarkers((prev) => ({
            ...prev,
            [ch]: [...(prev[ch] ?? []), { atCount: appendCntRef.current[ch] ?? 0, sensor: String(c.sensor_id ?? ""), label }],
          }));
        }
        primed = true;
      } catch { /* 게이트 오프라인 — 다음 주기 */ }
    };
    poll();
    const t = setInterval(poll, 5000);
    return () => { stopped = true; clearInterval(t); };
  }, []);

  // 플릿 웨이퍼 틱 — 간격 = 챔버 처리시간 ÷ 활성 챔버 수 (챔버 수 반응형).
  // 라운드로빈: 이번 wafer의 챔버만 센서 3종 시리즈가 1점씩 전진 → 펄스 태그와 차트 갱신이 1:1.
  useEffect(() => {
    const active = baseChambers.filter((c) => c.drift != null).map((c) => c.id);
    const intervalMs = Math.round((CHAMBER_WAFER_SEC * 1000) / Math.max(1, active.length));
    const Y_P95 = 1404; // 실데이터 C65 P95 — 위험 wafer 기준 (시트 예측과 동일 스케일)
    const t = setInterval(() => {
      if (liveRef.current) return;               // P5-4b — 실피드가 클럭을 잡으면 목업 정지
      const n = waferNo.current++;
      const chId = active[n % active.length];
      const chObj = baseChambers.find((c) => c.id === chId);
      const c65 = Math.round((chObj?.c65 ?? 980) + jit(7, n, 38)); // 챔버 예측 베이스 ± 자연 변동
      freshRef.current = true;
      setPulse((prev) => [
        { wafer: `W26-0714-${n}`, c65, warn: c65 >= Y_P95, ch: chId, lot: `L${231 + (n % 3)}` },
        ...prev.slice(0, 7),
      ]);
      setLastC65((m) => ({ ...m, [chId]: c65 }));
      setSeries((prev) => prev.map((bySensor, ci) => {
        const ch = baseChambers[ci];
        if (ch.id !== chId || ch.drift == null) return bySensor;
        return bySensor.map((ys, si) => [...ys.slice(1), genY(ch, ci, si, n)]);
      }));
      setWaferTick((v) => v + 1);
    }, intervalMs);
    return () => clearInterval(t);
  }, []);

  // 경과시간 — 60s에 +1분
  useEffect(() => {
    const t = setInterval(() => setAgeBump((v) => v + 1), 60000);
    return () => clearInterval(t);
  }, []);

  const views = useMemo<LiveChamberView[]>(() => {
    // 챔버·센서의 유효 y 시리즈 — raw 실곡선 우선, 없으면 목업 (센서 단위 폴백)
    const ysOf = (ch: Chamber, ci: number, si: number): { ys: number[]; sigs: number[] | null } => {
      const rawArr = rawLiveRef.current ? rawSeries[ch.id]?.[SENSOR_KEYS[si]] : undefined;
      if (rawArr) return { ys: rawArr.map((s) => clampY(yOf(s))), sigs: rawArr };
      return { ys: series[ci][si], sigs: null };
    };

    // fleet median (TTTM 관점) — 활성 챔버 "주시 센서" 헤드 σ의 중앙값
    const headSigs = baseChambers
      .map((ch, ci) => {
        const wi = SENSOR_KEYS.indexOf(watchOf(ch));
        const { sigs } = ysOf(ch, ci, wi);
        if (sigs) return sigs[sigs.length - 1];
        if (ch.drift == null) return null;
        return sigOf(series[ci][wi][N_POINTS - 1]);
      })
      .filter((v): v is number => v != null)
      .sort((a, b) => a - b);
    const fleetMedian = headSigs.length ? headSigs[Math.floor(headSigs.length / 2)] : 0;

    return baseChambers.map((ch, ci) => {
      const s = ci + 1;
      // C65 예측 = 해당 챔버의 "최신 스트림 wafer" 값 — Live Wafer와 단일 소스 (모순 방지)
      const c65 = ch.c65 == null ? null : (lastC65[ch.id] ?? ch.c65);

      // 센서 3종 차트 — 한 wafer = 한 측정 = 곡선 1점, 전 점 동결
      const step = xGrid[1] - xGrid[0];
      const chMarkers = limitMarkers[ch.id] ?? [];
      const appended = appendCntRef.current[ch.id] ?? 0;
      const charts: SensorChart[] = SENSOR_KEYS.map((sensor, si) => {
        const { ys, sigs } = ysOf(ch, ci, si);
        const real = sigs != null;
        const points = ys.map((y, i) => [xGrid[i], y] as [number, number]);
        const headSig = real
          ? sigs![sigs!.length - 1]
          : (ch.drift == null ? null : sigOf(points[points.length - 1][1]));
        const tailSig = real
          ? sigs![0]
          : (ch.drift == null ? null : sigOf(ys[0]));
        const isWatch = sensor === watchOf(ch);
        const uclY = yOf(3);                       // 관리선 위치는 매핑 파생 — 목업 픽셀 상수(ch.ucl) 폐기
        const lclY = yOf(-3);

        // Nelson 밴드 = UCL(+3σ) 위반 연속 구간 전체 — 회복해도 과거 위반 기록은 남는다
        const nelsonBands: SensorChart["nelsonBands"] = [];
        if (real || ch.drift != null) {
          let runStart = -1;
          for (let i = 0; i < points.length; i++) {
            const viol = points[i][1] < uclY || points[i][1] > lclY;   // 양측 관리선 (UCL·LCL)
            if (viol && runStart < 0) runStart = i;
            if ((!viol || i === points.length - 1) && runStart >= 0) {
              const runEnd = viol ? i : i - 1;
              const open = viol && i === points.length - 1;
              nelsonBands.push({
                x1: Math.max(0, points[runStart][0] - step / 2),
                x2: open ? X_MAX : points[runEnd][0] + step / 2,
                open,
              });
              runStart = -1;
            }
          }
        }

        let delta: string;
        if (headSig == null || tailSig == null) delta = ch.deltaLabel;
        else if (isWatch && !real && !ch.deltaLabel.includes("TODAY")) delta = ch.deltaLabel; // 가한계 등 특수 문구 유지
        else {
          const d = headSig - tailSig;
          delta = `${d >= 0 ? "+" : ""}${d.toFixed(1)}σ TODAY · ${SENSOR_LABEL[sensor]}`;
        }

        // F2 마커 — 이 센서의 correction만, 승인 시점 wafer가 창 안에 있으면 x 배치
        const markers = real
          ? chMarkers
              .filter((m) => m.sensor === sensor)
              .map((m) => {
                const idx = N_POINTS - 1 - (appended - m.atCount);
                return idx >= 1 ? { x: xGrid[idx], label: m.label } : null;
              })
              .filter((m): m is { x: number; label: string } => m != null)
          : undefined;

        const meta = rawMeta[ch.id]?.[sensor];
        const u = SENSOR_UNIT[sensor];
        return {
          sensor, isWatch, points, nelsonBands, headSig, delta,
          headEng: real
            ? (meta ? `${meta.eng.toFixed(u.dec)} ${u.unit}` : null)
            : (headSig == null ? null : engOf(ch, sensor, headSig)),
          uclEng: real ? null : engOf(ch, sensor, 3),
          real,
          limitVer: real ? (meta?.ver ?? null) : null,
          markers,
        };
      });

      // 도면 센서 수치 = 차트 헤드와 단일 소스 (σ 위치 → 공학단위 환산 + 미세 지터) — 차트/도면 모순 방지
      const headOf = (k: SensorKey) => charts.find((c) => c.sensor === k)?.headSig ?? 0;
      const engNum = (k: SensorKey) =>
        ch.sensors == null ? 0 : ch.sensors[SENSOR_CENTER[k]] + headOf(k) * SENSOR_UNIT[k].sig;
      const engReal = (k: SensorKey): number | null => {
        const m = rawMeta[ch.id]?.[k];
        return m ? m.eng : null;
      };
      const sensors = ch.sensors == null && !rawLiveRef.current
        ? { c11: "—", c32: "—", c17: "—", c4: "—" }
        : {
            c4: `${(engReal("C4") ?? engNum("C4") + jit(s * 9, tick, 0.3)).toFixed(1)} sccm`,
            c11: `${(engReal("C11") ?? engNum("C11") + jit(s * 3, tick, 0.1)).toFixed(1)} V`,
            c17: `${(engReal("C17") ?? engNum("C17") + jit(s * 7, tick, 0.08)).toFixed(1)}°C`,
            c32: `${Math.max(0, (ch.sensors?.c32 ?? 0) + jit(s * 5, tick, 0.4)).toFixed(0)} W`,
          };

      const watchChart = charts.find((c) => c.isWatch) ?? charts[0];
      const headSig = watchChart.headSig;
      const fd = headSig == null ? null : headSig - fleetMedian;
      const fleetDelta = fd == null ? "—" : `${fd >= 0 ? "+" : ""}${fd.toFixed(1)}σ`;
      const nelsonLabel = ch.nelson ? `${ch.nelson} · ${12 + ageBump}m 지속` : null;
      const sigChip = ch.tagOverride ?? (headSig != null ? `${headSig.toFixed(1)}σ` : "—");

      return {
        ch,
        sigChip,
        drift: headSig == null ? "—" : headSig.toFixed(2),
        driftSigma: headSig == null ? "—" : headSig.toFixed(1),
        c65: c65 == null ? "—" : c65.toLocaleString(),
        sensors,
        charts,
        fleetDelta,
        nelsonLabel,
      };
    });
  }, [tick, waferTick, series, ageBump, lastC65, rawSeries, rawMeta, limitMarkers]);

  const liveIncidents = useMemo(
    () => baseIncidents.map((inc) => ({
      ...inc,
      age: fmtAge(inc.ageMin + ageBump),
      ageM: inc.ageMin + ageBump,
    })),
    [ageBump],
  );

  const pulseView = useMemo(() => {
    const fresh = freshRef.current;
    freshRef.current = false;
    return pulse.map((p, i) => ({ ...p, fresh: fresh && i === 0 }));
  }, [pulse]);

  const ch3 = views.find((v) => v.ch.id === "CH3");
  // 2026-08-03 정직화 (PM 상황판 §3 ①). 옛 코드: `tick % 4 === 1 ? "1s" : "0s"`.
  // 실 피드와 아무 관계 없는 로컬 2초 카운터였다 — 스택이 통째로 죽어도 배지는 "0s/1s" 를
  // 계속 번갈아 찍었다. 8/13 에 데이터가 끊겨도 화면은 끝까지 건강해 보였을 자리다.
  // 지금은 마지막 실 이벤트(SSE wafer · raw append) 와의 실제 간격이고,
  // 실피드가 한 번도 안 붙었으면 null 을 낸다. (재계산 클럭 = 2초 tick 리렌더)
  const lastLiveMs = lastLiveMsRef.current;
  const lag = lastLiveMs == null
    ? null
    : `${Math.max(0, Math.round((Date.now() - lastLiveMs) / 1000))}s`;

  return {
    views,
    incidents: liveIncidents,
    pulse: pulseView,
    lag,
    worst: ch3 ? `${ch3.driftSigma}σ` : "—",
  };
}
