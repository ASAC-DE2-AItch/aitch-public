import { useEffect, useState } from "react";
import { fetchKpi, type KpiResponse } from "../api/client";

/** KPI 폴링 주기(ms). 정본은 `config/params.yaml` `operations.dashboard_poll_sec: 5` 이지만
 *  프론트가 params 를 못 읽어 상수로 둔다 — 그 키를 바꾸면 여기(와 QualScreen·Disposition)도 함께. */
const POLL_MS = 5000;
/** 이 시간(폴링 3회분) 넘게 새 응답이 없으면 **신선하지 않다**고 본다. */
const STALE_MS = POLL_MS * 3;

export interface KpiView {
  /** 마지막으로 성공한 응답. 한 번도 못 받았으면 null → 호출부가 목업 폴백. */
  data: KpiResponse | null;
  /** 값은 있는데 갱신이 끊겼는가. **배지가 거짓말하지 않게 하는 신호.** */
  stale: boolean;
  /** 마지막 성공 시각(ms). 배지 tooltip 용. */
  lastOkAt: number | null;
}

// ── 모듈 싱글톤 ──────────────────────────────────────────────────────────────
//  훅으로 나눠 놓기만 하면 **상태는 안 공유된다** — 호출부마다 자기 useState·타이머를 갖는다.
//  그래서 ⓐ 화면을 옮길 때마다 캐시가 날아가 목업이 한 번 번쩍이고(라이브 3 → 목업 12 → 라이브 3)
//  ⓑ Copilot 독·DecisionQueue 처럼 **상시 떠 있는** 소비자가 있으면 같은 순간에 서로 다른 숫자가 뜬다.
//  값을 모듈에 두고 구독만 훅으로 나눠, 소비자가 몇이든 **하나의 값·하나의 타이머**가 되게 한다.
let cache: KpiResponse | null = null;
let lastOkAt: number | null = null;
const subscribers = new Set<(v: KpiView) => void>();
let timer: ReturnType<typeof setTimeout> | null = null;
// **진행 중 표시.** `timer` 는 fetch 가 끝나야 채워져서, 그 사이엔 계속 null 이다 —
//   그 창에서 시작 가드(`timer === null`)가 뚫린다. React StrictMode 는 effect 를
//   create→destroy→create 로 두 번 도니 개발 중엔 **반드시** 걸려 폴링 체인이 둘이 되고,
//   하나는 핸들이 덮여 취소도 안 된다(요청·DB 조회가 2배).
let pulling = false;

const snapshot = (): KpiView => ({
  data: cache,
  stale: lastOkAt != null && Date.now() - lastOkAt > STALE_MS,
  lastOkAt,
});

const broadcast = () => subscribers.forEach((fn) => fn(snapshot()));

async function pull(): Promise<void> {
  if (pulling) return;                 // 체인 중복 방지 (위 주석)
  pulling = true;
  try {
    cache = await fetchKpi();
    lastOkAt = Date.now();
  } catch {
    /* 마지막 값 유지 — 호출부는 stale 로 판단한다. KPI 는 부가 정보라 화면을 죽이지 않는다. */
  } finally {
    pulling = false;
  }
  broadcast();
  // **자기 예약**(setInterval 아님) — interval 은 앞 요청이 안 끝나도 다음을 쏴서 응답이
  //   추월할 수 있다. 그러면 **오래된 값이 나중에 도착해** 카운트가 거꾸로 가고 스파크라인이
  //   튄다(에러 없음). 응답을 받은 뒤에 다음을 잡으면 동시 요청이 원천적으로 하나다.
  if (subscribers.size > 0) timer = setTimeout(pull, POLL_MS);
  else timer = null;
}

/**
 * `GET /kpi/agent` 구독 — S8(KPI 화면)·S1(DecisionQueue)·Copilot 독이 **같은 값**을 본다.
 *
 * 실패·미배선(503)은 마지막 성공값을 유지하고 `stale=true` 로 알린다. 값이 아예 없으면
 * `data=null` 이라 호출부가 목업으로 폴백한다(P5-4 시연 안전망).
 */
export function useKpi(): KpiView {
  const [view, setView] = useState<KpiView>(snapshot);

  useEffect(() => {
    subscribers.add(setView);
    setView(snapshot());                 // 재마운트 즉시 마지막 값 — 목업 번쩍임 방지
    if (subscribers.size === 1 && timer === null && !pulling) pull();
    // 값이 멈춰도 stale 은 시간이 지나면 참이 돼야 한다 — 폴링과 별개로 재평가한다
    const tick = setInterval(() => setView(snapshot()), POLL_MS);
    return () => {
      subscribers.delete(setView);
      clearInterval(tick);
      if (subscribers.size === 0 && timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    };
  }, []);

  return view;
}

/** 챔버 ID 표시 축약 — `SIM_CH_3` → `CH3` (QualScreen `chOf` 와 같은 규칙). */
export const shortChamber = (c: string): string => String(c ?? "").replace(/^SIM_?CH_?/, "CH");

/** 발표용 센서명 — **C코드가 정본**이고 표시층에서만 바꾼다(헌법 6-4).
 *
 *  `useLiveFdc.ts` 에도 같은 표(`SENSOR_LABEL`)가 있으나 **export 가 아니고 그 파일은 A 소유**라
 *  가져오지 않는다. 표를 늘려야 하면 `docs/데이터_사전_v1.md` 파생인 `config/sensor_map.yaml`
 *  이 정본이고, 여기와 저기가 **둘 다 그 사본**이다 — 임의로 이름을 지어내지 말 것.
 *  ※ 도면 콜아웃 3종만 있는 이유: 그게 지금 표가 가진 전부다. 파레토에는 C31·C61·C62 등이
 *    올라오는데, 없는 이름을 만들면 그게 곧 발표용 이름의 두 번째 정본이 된다. */
const SENSOR_LABEL: Record<string, string | undefined> = {
  C4: "C4 GAS FLOW", C11: "C11 DC BIAS", C17: "C17 TEMP",
};

/** 표시명 조회 — **매핑 없으면 C코드 그대로**. 타입이 `string | undefined` 인 덕에
 *  `?? code` 가 죽은 코드로 오해받지 않는다(그러면 매핑 없는 센서가 literal `undefined` 로 렌더된다). */
export const sensorLabel = (code: string): string => SENSOR_LABEL[code] ?? code;
