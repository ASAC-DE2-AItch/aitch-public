import { useEffect, useRef, useState, type ReactNode } from "react";
import { actions, type ActionItem } from "../mock/fdc";

/** 조치 & 효과 — closed loop의 '검증' 단계: 승인된 조치가 실제로 효과를 냈는지 (OOCAP 마지막 단계) */
const statusMeta: Record<ActionItem["status"], { label: string; cls: string }> = {
  applied: { label: "APPLIED", cls: "applied" },
  verifying: { label: "VERIFYING", cls: "verifying" },
  confirmed: { label: "CONFIRMED", cls: "confirmed" },
};

/** 넘치는 텍스트를 자르지 않고 순환 표시 — 끝까지 흐른 뒤 처음으로 복귀 (안 넘치면 정지) */
function Scrolly({ className, children }: { className: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [over, setOver] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setOver(Math.max(0, el.scrollWidth - el.clientWidth));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [children]);
  return (
    <div ref={ref} className={className + " scrolly"}>
      <span
        className={over > 0 ? "mq" : undefined}
        style={over > 0 ? ({ "--mq": `-${over + 6}px` } as React.CSSProperties) : undefined}
      >
        {children}
      </span>
    </div>
  );
}

export default function ActionsCard({ onExpand, onQual }: { onExpand: () => void; onQual: () => void }) {
  return (
    <div className="card acts">
      <div className="chead">
        <span className="t">Actions & Effect</span>
        <span className="cap num">CLOSED LOOP · {actions.length} APPLIED</span>
        <button className="ex" aria-label="전체 이력 — Approvals" title="전체 이력 — Approvals" onClick={onExpand}>
          <svg className="ic" viewBox="0 0 24 24" style={{ width: 14, height: 14 }}>
            <path d="M15 4h5v5M9 20H4v-5M20 4l-6 6M4 20l6-6" />
          </svg>
        </button>
      </div>
      <div className="ascroll">
      {actions.map((a) => {
        const m = statusMeta[a.status];
        return (
          <div
            className="arow"
            key={a.id}
            onClick={() => (a.type === "MNT" ? onQual() : onExpand())}
            style={{ cursor: "pointer" }}
            title={a.type === "MNT" ? "Qual 판정 (S7)" : "조치 이력 (S4)"}
          >
            <span className={"atype " + a.type.toLowerCase()}>{a.type}</span>
            <div className="abd">
              <div className="at">{a.title}</div>
              <Scrolly className="asub num">{a.chamber} · {a.at} · {a.id}</Scrolly>
              <div className="afx num">
                {a.progress && (
                  <>
                    <span className="abar">
                      <i
                        className={m.cls}
                        style={{ width: `${Math.round((a.progress[0] / a.progress[1]) * 100)}%` }}
                      />
                    </span>
                    <span className="acnt">{a.progress[0]}/{a.progress[1]} WF</span>
                  </>
                )}
                <Scrolly className="am">{a.metric}</Scrolly>
              </div>
            </div>
            <span className="qlead" />
            <span className={"apill " + m.cls}>{m.label}</span>
          </div>
        );
      })}
      </div>
    </div>
  );
}
