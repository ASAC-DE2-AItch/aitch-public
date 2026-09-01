// Gateway API 클라이언트 — 모든 호출은 /api/* (Vite 프록시 → FastAPI 8000)

export interface HealthResponse {
  status: string;
  ts: string;
  components: Record<string, string>;
  last_data_received_at: string | null;
}

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch("/api/health");
  if (!res.ok) throw new Error(`health ${res.status}`);
  return res.json();
}

export interface PushEvent {
  type: string;
  ts: string;
  pending_count: number;
}

/** SSE 구독 (S0 상단바: heartbeat·pending badge). 반환값 호출 시 구독 해제. */
export function subscribeEvents(onEvent: (e: PushEvent) => void, onError: () => void): () => void {
  const es = new EventSource("/api/events");
  es.onmessage = (m) => onEvent(JSON.parse(m.data) as PushEvent);
  es.onerror = () => onError();
  return () => es.close();
}

/** S8 KPI (gateway `GET /kpi/agent`). S8 화면·S1 DecisionQueue·Copilot 독이 같은 응답을 쓴다. */
export interface KpiResponse {
  today: number;                       // 창 내 승인 '처리'(결정 완료) 건수
  avg_resp_min: number | null;         // 평균 응답(MTTA, **분·소수 1자리**). 처리 0건이면 null
  /** 결정 분포 — 화면정의서 S8 의 「추천 채택률/수정률/반려율」 재료.
   *  **합 = `today`** (같은 스캔·같은 조건)라 비율의 분모는 언제나 `today` 하나다.
   *  비율이 아니라 건수인 이유: 분모 0(리셋 직후)에서 나눗셈을 표시층 한 곳으로 모은다. */
  decisions: { approved: number; modified: number; rejected: number; escalated: number };
  pareto: { sensor: string; n: number }[];   // 센서는 **C코드** — 표시명 변환은 프론트(헌법 6-4)
  chambers: { id: string; n: number }[];     // 챔버별 **인시던트** 수 (알람 아님)
  trend: number[];                     // 처리 추이 — 데이터 존재 범위를 등분한 막대
  window_hours: number;                // 집계 창(시간) — 각주 표기용
  /** 원천 데이터의 마지막 시각(ISO). **창이 비었을 때 「리셋 직후」와 「데이터가 낡음」을
   *  가르는 유일한 단서**다 — 없으면 둘 다 그냥 0 으로 보인다. 데이터가 아예 없으면 null. */
  data_last_at: string | null;
}

export async function fetchKpi(): Promise<KpiResponse> {
  const res = await fetch("/api/kpi/agent");
  if (!res.ok) throw new Error(`kpi ${res.status}`);
  const j = await res.json();
  // ⚠️ **형태를 확인하고 넘긴다.** `res.ok` 만 보면 200 인데 본문이 다른 경우(구버전
  //   게이트웨이·프록시가 끼워넣은 `{detail:…}`)에 `live.pareto.map` 이 렌더 중 터진다.
  //   앱에 ErrorBoundary 가 없어서 그 throw 는 **화면 전체를 하얗게** 만든다 —
  //   "KPI 는 부가 정보라 화면을 죽이지 않는다"(useKpi)는 약속이 여기서 깨진다.
  //   여기서 던지면 `pull()` 의 catch 가 받아 목업 폴백으로 조용히 내려간다.
  if (!j || typeof j.today !== "number" || !Array.isArray(j.pareto)
      || !Array.isArray(j.chambers) || !Array.isArray(j.trend) || !j.decisions) {
    throw new Error("kpi: 예상과 다른 응답 형태");
  }
  return j as KpiResponse;
}
