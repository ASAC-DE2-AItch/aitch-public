import { varStats } from "../mock/fdc";

export default function VarBar({ worst, onGo }: { worst: string; onGo: () => void }) {
  return (
    <div className="card varrow">
      <div style={{ flex: "none" }} title={varStats.basis}>
        <div className="vlb">VALUE AT RISK</div>
        <div className="vbig num">{varStats.amount}</div>
      </div>
      <div className="vdiv" />
      <div className="vmini"><div className="k">HOLD</div><div className="v num">{varStats.hold}</div></div>
      <div className="vmini"><div className="k">SCRAP</div><div className="v num">{varStats.scrap}</div></div>
      <div className="vmini"><div className="k">VERIFY</div><div className="v num">{varStats.verify}</div></div>
      <div className="vmini"><div className="k">WORST</div><div className="v num" style={{ color: "var(--pink)" }}>{worst}</div></div>
      <button className="gopill" style={{ marginLeft: "auto" }} onClick={onGo}>
        Disposition
        <svg className="ic" viewBox="0 0 24 24" style={{ width: 13, height: 13 }}><path d="M5 12h14M13 6l6 6-6 6" /></svg>
      </button>
    </div>
  );
}
