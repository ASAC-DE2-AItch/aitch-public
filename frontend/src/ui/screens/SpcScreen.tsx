import { useEffect, useMemo, useState } from "react";
import type { LiveChamberView } from "../../live/useLiveFdc";
import { sigOf } from "../../live/useLiveFdc";
import SpcFlow from "../SpcFlow";

/** S2 SPC/TTTM — 센서 상세 + 챔버간 비교(현업 Box Plot 방식 — 멘토 확인) */
// ⚠️ 목업 예시다 — 실 이력이 아니다 (2026-08-03 명시).
// control_limits 실측은 v13 까지 올라가 있는데 여기는 v1~v3 로 고정돼 있었고,
// 카드에 아무 표식이 없어 실이력처럼 보였다. limit_corrections 조회 API 가 아직 없어
// 배선은 못 하므로, 배선 전까지는 화면에 ○ MOCK 을 달아 정직하게 둔다.
const LIMIT_HIST = [
  { v: "v1", at: "06/30", by: "B2-1 시딩", note: "초기 실력치 (정착 세그먼트)" },
  { v: "v2", at: "07/01·05", by: "정기 리캘리 (자동)", note: "0.5σ 이내 · trigger=periodic" },
  { v: "v3", at: "07/12", by: "승인 재설정", note: "C11 +4.1% · INC-CH3-001 · 소급 채점 통과" },
];

export default function SpcScreen({
  views, selectedId, onSelect,
}: { views: LiveChamberView[]; selectedId: string; onSelect: (id: string) => void }) {
  const view = views.find((v) => v.ch.id === selectedId) ?? views[0];

  // P6-2 S2 — 가한계 Phase 조회: control_limits.trigger_type='provisional'인 챔버는
  // 광폭 관리선 + 예측 '추정'(R9 프라이어 bias). /api/limits/active 5초 폴링, 실패=무표시(안전망).
  const [provCh, setProvCh] = useState<Set<string>>(new Set());
  useEffect(() => {
    let alive = true;
    const chOf = (c: unknown) => String(c ?? "").replace(/^SIM_?CH_?/, "CH");
    const pull = async () => {
      try {
        const r = await fetch("/api/limits/active", { signal: AbortSignal.timeout(2500) });
        if (!r.ok) return;
        const lim = (await r.json())?.limits ?? [];
        if (!alive) return;
        const s = new Set<string>();
        for (const l of lim) if (l?.trigger_type === "provisional") s.add(chOf(l.chamber_id));
        setProvCh(s);
      } catch { /* 무표시 (안전망) */ }
    };
    pull();
    const t = setInterval(pull, 5000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  const provisional = provCh.has(selectedId);

  // TTTM 박스플롯 — 각 챔버 주시 센서 곡선의 σ 분포 (라이브)
  const boxes = useMemo(() => views.map((v) => {
    const w = v.charts.find((c) => c.isWatch) ?? v.charts[0];
    const sigs = w.points.map(([, y]) => sigOf(y)).sort((a, b) => a - b);
    const q = (p: number) => sigs[Math.floor(p * (sigs.length - 1))];
    return { id: v.ch.id, sev: v.ch.sev, min: q(0.02), q1: q(0.25), med: q(0.5), q3: q(0.75), max: q(0.98), pm: v.ch.drift == null };
  }), [views]);
  const fleetMed = useMemo(() => {
    const m = boxes.filter((b) => !b.pm).map((b) => b.med).sort((a, b) => a - b);
    return m[Math.floor(m.length / 2)];
  }, [boxes]);
  const X = (sig: number) => 40 + ((sig + 0.5) / 4) * 300; // -0.5σ~3.5σ → 40~340

  return (
    <div className="scr">
      <div className="scrh">
        <div>
          <div className="t">
            SPC Charts
            {provisional && <span className="lgi" style={{ marginLeft: 8, color: "var(--amber-deep, #b8720f)" }}>◐ 가한계·추정</span>}
          </div>
          <div className="c num">
            CHAMBER × RECIPE × STEP · 관리선 정본 = control_limits (is_active)
            {provisional && " · 광폭 관리선(provisional) · 예측 추정 bias — Phase 2 재산정 대기"}
          </div>
        </div>
        <div className="fchips">
          {views.map((v) => (
            <button key={v.ch.id} className={"fchip" + (v.ch.id === selectedId ? " on" : "")} onClick={() => onSelect(v.ch.id)}>
              {v.ch.id}{provCh.has(v.ch.id) ? " ◐" : ""} <span className="num" style={{ opacity: .6 }}>{v.sigChip}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1.25fr .75fr" }}>
        <div className="col">
          <SpcFlow v={view} />
          <div className="card">
            <div className="chead"><span className="t">관리선 이력</span><span className="cap num">{view.ch.id} · LIMIT {view.ch.limitVer} · 이력 ○ MOCK</span></div>
            {provisional && (
              <div className="lhrow" style={{ background: "var(--amber-soft, #fef3e0)" }}>
                <span className="lv num cur" style={{ color: "var(--amber-deep, #b8720f)" }}>가한계</span>
                <span className="num" style={{ color: "var(--faint)", width: 52 }}>진행중</span>
                <span style={{ fontWeight: 500 }}>provisional (B6-3)</span>
                <span className="num" style={{ marginLeft: "auto", color: "var(--label)", fontSize: 10.5 }}>광폭 관리선 · 정착 1,000장 후 Phase 2 재산정</span>
              </div>
            )}
            {LIMIT_HIST.map((h) => (
              <div className="lhrow" key={h.v}>
                <span className={"lv num" + (view.ch.limitVer.startsWith(h.v) ? " cur" : "")}>{h.v}</span>
                <span className="num" style={{ color: "var(--faint)", width: 52 }}>{h.at}</span>
                <span style={{ fontWeight: 500 }}>{h.by}</span>
                <span className="num" style={{ marginLeft: "auto", color: "var(--label)", fontSize: 10.5 }}>{h.note}</span>
              </div>
            ))}
            <div className="scrfoot num" style={{ padding: "6px 16px 12px" }}>재설정 이력 = limit_corrections (delta_sigma 원장 · A2 0.5σ / A3 1.0σ)</div>
          </div>
        </div>

        <div className="col">
          <div className="card">
            <div className="chead"><span className="t">TTTM — 챔버간 비교</span><span className="cap num">FLEET MEDIAN 기준 · BOX PLOT</span></div>
            <svg viewBox="0 0 380 210" style={{ width: "100%", display: "block", padding: "4px 8px 10px" }}>
              {[0, 1, 2, 3].map((s) => (
                <g key={s}>
                  <line x1={X(s)} y1="8" x2={X(s)} y2="188" stroke="var(--bpgrid)" strokeWidth="1" />
                  <text x={X(s)} y="200" textAnchor="middle" fontSize="9" fill="var(--faint)" fontFamily="monospace">{s}σ</text>
                </g>
              ))}
              <line x1={X(fleetMed)} y1="8" x2={X(fleetMed)} y2="188" stroke="var(--mint)" strokeWidth="1.6" strokeDasharray="5 4" />
              <text x={X(fleetMed)} y="16" textAnchor="middle" fontSize="8.5" fill="var(--mint-deep)" fontFamily="monospace">FLEET MED</text>
              {boxes.map((b, i) => {
                const y = 30 + i * 26;
                const col = b.pm ? "var(--faint)" : b.sev === "critical" ? "var(--pink)" : b.sev === "warning" ? "var(--alarm)" : "#9db4c4";
                return (
                  <g key={b.id} opacity={b.pm ? 0.35 : 1}>
                    <text x="8" y={y + 4} fontSize="10" fontWeight="600" fill="var(--ink-strong)" fontFamily="monospace">{b.id}</text>
                    <line x1={X(b.min)} y1={y} x2={X(b.max)} y2={y} stroke={col} strokeWidth="1.2" />
                    <rect x={X(b.q1)} y={y - 6} width={Math.max(2, X(b.q3) - X(b.q1))} height="12" fill={col} opacity=".18" stroke={col} strokeWidth="1.2" rx="2" />
                    <line x1={X(b.med)} y1={y - 6} x2={X(b.med)} y2={y + 6} stroke={col} strokeWidth="2.2" />
                  </g>
                );
              })}
            </svg>
            <div className="scrfoot num" style={{ padding: "0 16px 12px" }}>가한계 챔버 = median 제외·판정 유예 (B6-3) · 역방향 룰 ≥3/4 (B3)</div>
          </div>

          <div className="card">
            <div className="chead"><span className="t">윈도우·구간 규칙</span></div>
            <div className="krow"><b>SETTLED</b><span>판정 기본 윈도우 — C42 기반 2윈도우 분리</span></div>
            <div className="krow"><b>TRANSIENT</b><span>과도 구간 — 판정 제외 (A8·F12)</span></div>
            <div className="krow"><b>KEEP*</b><span>분위수 관리선 6그룹 (헌법 1-1 예외 2) — σ-존 룰 제외</span></div>
            <div className="krow"><b>PROV</b><span>가한계 Phase 1 — 광폭 4~5σ · 추세 룰만 (CH4)</span></div>
          </div>
        </div>
      </div>
    </div>
  );
}
