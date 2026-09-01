import type { LiveChamberView } from "../live/useLiveFdc";

/** 좌: 백지 도면(회색 격자 + 하늘 잉크) / 우: Drift·C65(AI)·SHAP·Nelson — 라이브 값 */
export default function ChamberSheet({ v, onQual }: { v: LiveChamberView; onQual?: () => void }) {
  const ch = v.ch;
  const chNum = ch.id.replace("CH", "0");
  return (
    <div className="sheet">
      <div className="strip">
        <span className="dwg">DWG · CH{chNum} ETCH CHAMBER · REV.{ch.rev}</span>
        <span
          className={"runchip " + ch.runState.toLowerCase()}
          onClick={(ch.runState === "PM" || ch.tagOverride === "PROV") && onQual ? onQual : undefined}
          style={(ch.runState === "PM" || ch.tagOverride === "PROV") && onQual ? { cursor: "pointer" } : undefined}
          title={ch.runState === "PM" ? "Qual 판정 보기 (S7)" : ch.tagOverride === "PROV" ? "가한계 — Qual 판정·레짐 진행 (S7)" : undefined}
        >
          {ch.runState}{ch.runState === "PM" || ch.tagOverride === "PROV" ? " · QUAL →" : ""}
        </span>
        {ch.alarmText
          ? <span className="alarm" style={{ marginLeft: "auto" }}>{ch.alarmText}</span>
          : <span className="dwg" style={{ color: "var(--faint)", marginLeft: "auto" }}>NO ACTIVE ALARM</span>}
      </div>

      <div className="bodyx">
        <div className="draw">
          <div className="bp">
            <svg viewBox="-16 0 332 194">
              <line x1="150" y1="12" x2="150" y2="34" stroke="var(--ink)" strokeWidth="1" />
              <polygon points="150,34 146,26 154,26" fill="var(--ink)" />
              <text x="150" y="9" fill="var(--ink-strong)" fontSize="8" textAnchor="middle" fontFamily="monospace">GAS INLET · C4/C5</text>

              <rect x="78" y="40" width="144" height="150" rx="5" fill="none" stroke="var(--ink)" strokeWidth="1.4" />

              <line x1="96" y1="62" x2="204" y2="62" stroke="var(--ink)" strokeWidth="1.2" />
              {[104, 120, 136, 152, 168, 184].map((x) => (
                <line key={x} x1={x} y1="62" x2={x} y2="68" stroke="var(--ink)" strokeWidth="1" opacity=".75" />
              ))}
              <text x="150" y="58" fill="var(--label)" fontSize="8" textAnchor="middle" fontFamily="monospace">SHOWERHEAD</text>

              <circle cx="122" cy="92" r="1.6" fill="var(--ink)" /><circle cx="150" cy="86" r="1.6" fill="var(--ink)" /><circle cx="178" cy="94" r="1.6" fill="var(--ink)" />
              <circle cx="136" cy="104" r="1.6" fill="var(--ink)" /><circle cx="164" cy="102" r="1.6" fill="var(--ink)" /><circle cx="150" cy="112" r="1.6" fill="var(--ink)" />
              <text x="228" y="92" fill="var(--label)" fontSize="8" fontFamily="monospace">PLASMA</text>
              <line x1="206" y1="90" x2="222" y2="90" stroke="var(--faint)" strokeWidth="0.8" strokeDasharray="2 2" />

              <rect x="112" y="138" width="76" height="12" rx="1" fill="none" stroke="var(--ink)" strokeWidth="1.2" />
              <line x1="116" y1="136" x2="184" y2="136" stroke="var(--ink-strong)" strokeWidth="2" />
              <line x1="150" y1="150" x2="150" y2="190" stroke="var(--ink)" strokeWidth="1" />
              <path d="M144 180 l6 -5 6 5" fill="none" stroke="var(--ink)" strokeWidth="1" />
              <text x="150" y="168" fill="var(--label)" fontSize="8" textAnchor="middle" fontFamily="monospace">WAFER · ESC</text>

              <line x1="222" y1="150" x2="266" y2="150" stroke="var(--ink)" strokeWidth="1" />
              <polygon points="266,150 258,146 258,154" fill="var(--ink)" />
              <text x="238" y="162" fill="var(--ink-strong)" fontSize="8" fontFamily="monospace">PUMP</text>

              <line x1="24" y1="92" x2="120" y2="92" stroke="var(--faint)" strokeWidth="0.8" />
              <circle cx="120" cy="92" r="2" fill={ch.c4Alarm ? "var(--alarm)" : "var(--ink)"} />
              {/* 위계 통일: 범주(라벨)는 항상 얇고 옅게, 수치만 굵게 — 경보 시 수치 색만 전환 */}
              <text x="-8" y="86" fill="var(--label)" fontSize="8.5" fontFamily="monospace">C4 GAS</text>
              <text x="-8" y="106" fill={ch.c4Alarm ? "var(--alarm)" : "var(--text)"} fontSize="13" fontFamily="monospace" fontWeight="700">{v.sensors.c4}</text>

              <line x1="24" y1="142" x2="112" y2="142" stroke="var(--faint)" strokeWidth="0.8" />
              <circle cx="112" cy="142" r="2" fill="var(--ink)" />
              <text x="-8" y="136" fill="var(--label)" fontSize="8.5" fontFamily="monospace">C11 BIAS</text>
              <text x="-8" y="156" fill="var(--text)" fontSize="13" fontFamily="monospace" fontWeight="700">{v.sensors.c11}</text>

              <line x1="222" y1="118" x2="306" y2="118" stroke="var(--faint)" strokeWidth="0.8" />
              <circle cx="222" cy="118" r="2" fill="var(--ink)" />
              <text x="310" y="113" textAnchor="end" fill="var(--label)" fontSize="8.5" fontFamily="monospace">C17 TEMP</text>
              <text x="310" y="133" textAnchor="end" fill="var(--text)" fontSize="13" fontFamily="monospace" fontWeight="700">{v.sensors.c17}</text>
            </svg>
          </div>
        </div>

        <div className="sidep">
          <div style={{ display: "flex", gap: 8, flex: 1 }}>
            <div className="pnl">
              <div className="k">Drift</div>
              <div className={"v num" + (ch.sev === "critical" ? " warn" : "")}>
                {v.drift}<small> σ</small>
              </div>
              <div className="fleetd num" title="fleet median 대비 편차 (TTTM)">FLEET Δ {v.fleetDelta}</div>
            </div>
            <div className="pnl">
              <div className="k">
                C65 예측 <span style={{ color: "var(--mint)", fontSize: 9 }}>● AI</span>
              </div>
              <div className="v num" style={{ color: "var(--mint-deep)" }}>{v.c65}</div>
              {ch.predConf != null && (
                <div className="fleetd num" title="예측 신뢰도">CONF {ch.predConf.toFixed(2)}</div>
              )}
            </div>
          </div>

          <div className="pnl line" style={{ flex: 1.35, justifyContent: "flex-start" }}>
            <div className="k" style={{ marginTop: 2 }}>SHAP 상위 기여</div>
            {ch.shap.length === 0 && (
              <div className="shr" style={{ color: "var(--faint)" }}>PM 중 — 추론 없음</div>
            )}
            {ch.shap.map((s) => (
              <div key={s.code}>
                <div className="shr">
                  <span>{s.name} <b>{s.code}</b></span>
                  <span className="sv num">{s.val.toFixed(2)}</span>
                </div>
                <div className="sbar"><i style={{ width: `${s.pct}%` }} /></div>
              </div>
            ))}
          </div>

          {v.nelsonLabel && (
            <div className="nelstrip">
              <div>{v.nelsonLabel.split(" · ").slice(0, 2).join(" · ")}</div>
              <div className="nsub">{v.nelsonLabel.split(" · ").slice(2).join(" · ")}</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
