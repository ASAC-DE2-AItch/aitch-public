// S1 히어로 — AURA 클론: 3D가 캔버스에 직접 부유(박스 없음) + 떠있는 미니 스탯 카드
import ChamberHero3D, { HotspotDef } from "./ChamberHero3D";
import { ChamberSummary, focusSensors } from "../mock/monitoring";

export default function HeroChamber({ ch }: { ch: ChamberSummary }) {
  const s = focusSensors(ch);
  const abnormal = ch.status === "WARNING" || ch.status === "CRITICAL";

  const hotspots: HotspotDef[] = [
    { num: 1, anchor: [0.68, -0.32, 0.3], alarm: s[0].alarm, label: `${s[0].label} ${s[0].code}`, value: s[0].value, unit: s[0].unit },
    { num: 2, anchor: [0.88, 0.7, 0.42], alarm: s[1].alarm, label: `${s[1].label} ${s[1].code}`, value: s[1].value, unit: s[1].unit },
    { num: 3, anchor: [-1.6, -0.56, 0.35], alarm: s[2].alarm, label: `${s[2].label} ${s[2].code}`, value: s[2].value, unit: s[2].unit },
  ];

  return (
    <div className="relative">
      {/* 헤더 라인 — 위젯 밖, 캔버스 위 (AURA 히어로는 박스 없음) */}
      <div className="mb-1 flex items-center gap-3 px-1">
        <span className="text-[15px] font-bold" style={{ color: "var(--text)" }}>
          {ch.chamberId.replace("SIM_", "")} 식각 챔버
        </span>
        <span className={`chip ${abnormal ? "chip-warn" : "chip-good"}`}>
          <span className="dot" style={{ background: abnormal ? "var(--warn)" : "var(--good)" }} />
          {abnormal ? ch.statusNote : "정상 가동"}
        </span>
        <span className="mono ml-auto text-[10.5px]" style={{ color: "var(--text-faint)" }}>
          quarter-section · live
        </span>
      </div>

      <ChamberHero3D abnormal={abnormal} hotspots={hotspots} height={440} />

      {/* 하단 바이탈 칩 (AURA 보조 지표) */}
      <div className="mt-1 flex gap-2 px-1">
        <span className="chip chip-neutral">rf cycle {(ch.rfProgress * 100).toFixed(0)}%</span>
        <span className="chip chip-neutral">vacuum 8.0 mTorr</span>
        <span className="chip chip-neutral">drift {ch.driftScore.toFixed(2)}</span>
      </div>
    </div>
  );
}
