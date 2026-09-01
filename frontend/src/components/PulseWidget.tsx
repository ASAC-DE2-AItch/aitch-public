// 라이브 펄스 위젯 — AURA 테이블 문법 (위젯 내 라운드 행)
import { UCL, WaferRow } from "../mock/monitoring";

const RISK_CHIP = { low: "chip-neutral", mid: "chip-warn", high: "chip-crit" } as const;

export default function PulseWidget({ stream, chamberFilter }: {
  stream: WaferRow[]; chamberFilter: string;
}) {
  return (
    <div className="widget widget-pad">
      <div className="flex items-center justify-between">
        <span className="w-label">라이브 펄스 · fdc.prediction</span>
        <span className="chip chip-neutral">UCL {UCL}</span>
      </div>
      <table className="mt-2 w-full border-collapse text-[12.5px]">
        <thead>
          <tr style={{ color: "var(--text-faint)" }}>
            {["wafer", "chamber", "C65", "risk", "수신"].map((h) => (
              <th key={h} className="pb-1.5 text-left text-[10.5px] font-semibold">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {stream.slice(0, 5).map((w, i) => (
            <tr key={w.waferId} className={i === 0 ? "stream-new" : ""}
              style={{
                color: "var(--text)",
                opacity: chamberFilter !== "ALL" && chamberFilter !== w.chamberId ? 0.35 : 1,
              }}>
              <td className="mono py-1.5 text-[11.5px] tabular-nums">{w.waferId}</td>
              <td className="py-1.5">{w.chamberId.replace("SIM_", "")}</td>
              <td className="py-1.5 font-bold tabular-nums">{w.predictedC65.toLocaleString()}</td>
              <td className="py-1.5"><span className={`chip ${RISK_CHIP[w.risk]}`} style={{ fontSize: 10, padding: "1px 8px" }}>{w.risk}</span></td>
              <td className="py-1.5 tabular-nums" style={{ color: "var(--text-faint)" }}>
                {new Date(w.ts).toLocaleTimeString("ko-KR", { hour12: false })}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
