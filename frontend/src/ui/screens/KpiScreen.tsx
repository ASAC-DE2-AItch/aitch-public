import { kpiExt, kpis } from "../../mock/fdc";
import { sensorLabel, shortChamber, useKpi } from "../../live/useKpi";
import LiveBadge from "../LiveBadge";

/** S8 KPI — 알람 위생·응답·Scorecard (알람 파레토 = 리뷰 확정 KPI 탭 배치)
 *
 *  2026-08-12 실배선 (PM 요청분): 처리 속도·파레토·챔버별·추이를 `GET /kpi/agent` 로 교체.
 *  실패/빈 = 목업 유지 (QualScreen·P5-4 와 같은 시연 안전망). 5초 폴링.
 *
 *  **Scorecard 는 목업 그대로다.** 그 카드의 정본은 `sim_events` 정답지 기반 자동 채점이고
 *  화면 각주가 *"채점 전용 — 파이프라인 조회 금지"* 로 선을 그어 뒀다. 실시간 조회 지표가
 *  아니라 검증 계측이라 배선 대상이 아니다 (PM 확인분).
 *
 *  구 스탯 카드 4장 중 **미탐·가성알람 2장을 뺐다** — 둘 다 조회로 만들 수 없다.
 *  미탐은 정답지(데모 `sim_events` / 공장 실측 C65)가 있어야 하고, 가성알람 −62% 는
 *  "재설정 전 대비" 두 시점 비교값이다. 미탐 지표 자체는 사라지지 않는다 —
 *  아래 Scorecard 의 M2·M8 이 정본이다(구 종합현황 화면의 역할 분담과 같다:
 *  *"오늘 = 얼마나 빨리 / Scorecard = 제대로 하고 있나"*).
 */
/** 카드에 그리는 최대 행 수. 백엔드는 여유분(8)까지 주고 **자르는 건 표시층**이다. */
const PARETO_ROWS = 5;

export default function KpiScreen() {
  // S1 DecisionQueue·Copilot 독과 **같은 모듈 캐시**를 본다 — 소비자가 몇이든 값이 하나다
  const { data: live, stale, lastOkAt } = useKpi();

  // 파레토 행의 key 는 **센서 코드**다 — 표시명은 바뀌는 층이라 신원으로 쓰면 안 된다
  const pareto = live
    ? live.pareto.map((p) => ({ id: p.sensor, k: sensorLabel(p.sensor), n: p.n }))
    : kpiExt.pareto.map((p) => ({ id: p.k, k: p.k, n: p.n }));
  const chambers = live
    ? live.chambers.map((c) => ({ id: c.id, k: shortChamber(c.id), n: c.n }))
    : kpiExt.chambers.map((c) => ({ id: c.id, k: c.id, n: c.n }));
  const trend = live ? live.trend : kpiExt.trend;
  const today = live ? String(live.today) : String(kpis.today);
  const resp = live ? (live.avg_resp_min != null ? `${live.avg_resp_min}m` : "—") : kpis.avgResp;

  // 채택률·반려율 — 화면정의서 S8 「Agent 약점 영역 식별」. 분모는 **처리 건수 하나**다.
  //   0건이면 `0%` 가 아니라 `—` 다 — 0% 는 "다 반려됐다"로 읽히지만 실제로는 "아직 없다"다.
  const d = live?.decisions;
  const rate = (n: number | undefined) =>
    live && d && live.today > 0 ? `${Math.round(((n ?? 0) / live.today) * 100)}%` : "—";
  const adopt = rate(d?.approved);
  const reject = rate(d ? d.rejected + d.escalated : undefined);

  // max 는 막대 비율의 분모다 — 0 이면 NaN% 가 style 로 들어가 막대가 통째로 사라진다
  //   (에러 없이 빈 카드가 된다). 리셋 직후가 정확히 그 상태라 반드시 하한을 둔다.
  const maxP = Math.max(...pareto.map((p) => p.n), 1);
  const maxC = Math.max(...chambers.map((c) => c.n), 1);
  const maxT = Math.max(...trend, 1);

  return (
    <div className="scr">
      <div className="scrh">
        <div>
          <div className="t">
            KPI
            <LiveBadge live={!!live} stale={stale} lastOkAt={lastOkAt} />
          </div>
          {/* 창 표기는 여기 — `window_hours` 는 챔버 카드뿐 아니라 **모든 지표**에 걸린다.
              카드 하나에만 붙여두면 그 카드만 24h 라는 뜻으로 읽힌다. */}
          <div className="c num">
            알람 위생 · 응답 · SCORECARD (Tier 1){live ? ` · 최근 ${live.window_hours}시간` : ""}
          </div>
        </div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
        {[
          // 창이 `window_hours`(기본 24h)라 「오늘」은 정확하지 않다 — 부제에 실제 창을
          //   적어두고 카드는 중립적으로 「처리」로 둔다(창을 8h 로 바꿔도 안 틀린다).
          ["처리", today, "건"],
          ["평균 응답", resp, "MTTA"],
          // 미탐·가성알람이 있던 자리 — 둘 다 조회로 못 만들어 뺐고, 화면정의서가 요구하던
          //   채택률/수정률/반려율로 채운다. 같은 스캔에서 나와 추가 쿼리가 없다.
          ["채택률", adopt, d ? `원안 ${d.approved} · 수정 ${d.modified}` : "원안 그대로 승인"],
          ["반려율", reject, d ? `반려 ${d.rejected} · 에스컬 ${d.escalated}` : "반려 + 에스컬레이션"],
        ].map(([k, v, s]) => (
          <div className="card statc" key={k as string}>
            <div className="cap num">{k}</div>
            <div className="big num">{v}</div>
            <div className="sub num">{s}</div>
          </div>
        ))}
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr 1fr" }}>
        <div className="card">
          <div className="chead"><span className="t">알람 파레토 — 센서</span><span className="cap num">TOP OFFENDER</span></div>
          {/* 빈 배열은 **정상 상태**다 — 리셋 직후(런북 A-2)가 정확히 그렇다. 행 0개로 두면
              헤더와 각주만 남아 고장난 카드로 보인다. */}
          {pareto.length ? pareto.slice(0, PARETO_ROWS).map((p) => (
            <div className="brow" key={p.id}>
              <span className="num bl">{p.k}</span>
              <span className="bbar"><i style={{ width: `${(p.n / maxP) * 100}%`, background: "var(--pink)" }} /></span>
              <span className="num bv">{p.n}</span>
            </div>
          )) : <div className="brow"><span className="num bl">—</span><span className="cap num">집계 대상 없음</span></div>}
          {/* 창이 비었을 때 「리셋 직후(정상)」인지 「데이터가 낡음(시뮬 멎음)」인지 —
              이 각주가 없으면 둘 다 그냥 0 으로 보인다(헌법 7장: 조용한 상태 노출). */}
          <div className="scrfoot num" style={{ padding: "4px 16px 12px" }}>
            {live && !pareto.length
              ? (live.data_last_at
                  ? `창 내 데이터 없음 — 마지막 데이터 ${new Date(live.data_last_at).toLocaleString("ko-KR")}`
                  : "데이터 없음 — 시뮬레이터 기동 전이거나 리셋 직후")
              : "오탐 관리 재료 — 상위 센서 관리선 점검 우선"}
          </div>
        </div>

        <div className="card">
          {/* 캡션이 「7일」이었다 — 한 라운드가 4~5분이라 달력 단위와 안 맞는다. 창은 응답의
              window_hours(기본 24h)이고, 데모는 매 회차 TRUNCATE 라 그 창이 곧 라운드다. */}
          <div className="chead"><span className="t">챔버별 인시던트</span><span className="cap num">{live ? "창 내" : "7일"}</span></div>
          {chambers.length ? chambers.map((c) => (
            <div className="brow" key={c.id}>
              <span className="num bl">{c.k}</span>
              <span className="bbar"><i style={{ width: `${(c.n / maxC) * 100}%`, background: c.n >= 8 ? "var(--pink)" : c.n >= 3 ? "var(--alarm)" : "#c3cdd6" }} /></span>
              <span className="num bv">{c.n}</span>
            </div>
          )) : <div className="brow"><span className="num bl">—</span><span className="cap num">인시던트 없음</span></div>}
          <div className="brow" style={{ paddingTop: 8 }}>
            {/* 구 「일별 처리 추이」 — 구간이 달력 일이 아니라 **데이터 존재 범위 등분**이라
                「일별」은 거짓이 된다. 스파크라인은 절대 시간축이 아니라 기간 내 모양을 본다. */}
            <span className="num bl" style={{ width: "auto" }}>처리 추이</span>
            <span className="spark">
              {trend.map((t, i) => (
                <i key={i} style={{ height: `${(t / maxT) * 100}%` }} />
              ))}
            </span>
          </div>
        </div>

        <div className="card">
          <div className="chead"><span className="t">Scorecard</span><span className="cap num">sim_events 자동 채점</span></div>
          {kpiExt.score.map((s) => (
            <div className="krow" key={s.id}>
              <b className="num" style={{ width: 36, color: "var(--faint)" }}>{s.id}</b>
              <span>{s.label}</span>
              <span className="num" style={{ marginLeft: "auto", fontWeight: 700 }}>{s.v}</span>
              <span className={"schip " + (s.pass ? "ok" : "warn")}>{s.pass ? "PASS" : "WIP"}</span>
            </div>
          ))}
          <div className="scrfoot num" style={{ padding: "4px 16px 12px" }}>정답지 = sim_events (채점 전용 — 파이프라인 조회 금지)</div>
        </div>
      </div>
    </div>
  );
}
