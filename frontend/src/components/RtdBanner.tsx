// S1 — RTD 배너 2종 (2026-08-06 자동 정지 승격 — 헌법 1-1 예외 4)
//   ① 자동 정지 발효 배너: /chambers/status 폴링 — inhibit 발효 중인 챔버 표시 (신규)
//   ② 정지 「제안」 배너: 경로① reference_suspect 연속 K (기존 — useRtd)
// 상태 파생 카드: 조건이 사라지면 스스로 내려간다.
// 토큰 주의(8/4 시연 실측): 이 앱의 styles.css 에는 --crit/--text-muted/.chip 이 없다.
//   라이브 트리 토큰(--pink / --pink-soft / --pink-deep / --faint / --mono)만 쓴다.
import { useRtd } from "../live/useRtd";
import { useChamberStatus } from "../live/useChamberStatus";
// ChamberStatus 형·폴링은 live/useChamberStatus.ts 공용 훅으로 이동 (2026-08-06 #75 — Header 와 공유)

export default function RtdBanner() {
  const st = useRtd();
  const chambers = useChamberStatus();
  const inhibited = chambers.filter((c) => c.inhibited);
  const showProposal = !!(st && st.proposed);
  if (!inhibited.length && !showProposal) return null;

  return (
    <>
      {/* ① 자동 정지 발효 — 예외 4: 정지는 자동, 해제는 Requal 승인 하류만 */}
      {inhibited.length > 0 && (
        <div
          style={{
            margin: "0 0 14px",
            padding: "12px 16px",
            background: "var(--pink-soft)",
            borderLeft: "4px solid var(--pink-deep)",
            boxShadow: "0 2px 10px rgba(23,50,74,.07)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ font: "700 12.5px var(--mono)", color: "var(--pink-deep)", letterSpacing: ".02em" }}>
              {inhibited.some((c) => c.scope === "equipment") ? (
                <>
                  ■ RTD 자동 정지 발효 — <span className="num">
                    {inhibited.find((c) => c.equipment_id)?.equipment_id ?? "장비"}
                  </span> <b>장비 단위</b> 생산 중단 (공용 설비 공통 원인 · {inhibited.length}챔버)
                </>
              ) : (
                <>
                  ■ RTD 자동 정지 발효 —{" "}
                  <span className="num">
                    {inhibited.map((c) => c.chamber_id.replace("SIM_", "")).join(" · ")}
                  </span>{" "}
                  챔버 생산 중단
                </>
              )}
            </span>
          </div>
          <div style={{ marginTop: 5, fontSize: 11, color: "var(--faint)" }}>
            {inhibited.map((c) => (
              <span key={c.chamber_id} className="num" style={{ marginRight: 10 }}>
                {c.chamber_id.replace("SIM_", "")} · {c.incident_id}
                {c.inhibited_at ? ` · ${new Date(c.inhibited_at).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" })}~` : ""}
                {` · uptime ${c.uptime_pct.toFixed(1)}%`}
              </span>
            ))}
            {" — 해제는 Requal 승인 후 자동 (수동 해제 경로 없음 · 헌법 1-1 예외 4)"}
          </div>
        </div>
      )}

      {/* ② 정지 제안 (경로① — 기존 유지) */}
      {showProposal && st && (
        <div
          style={{
            margin: "0 0 14px",
            padding: "12px 16px",
            background: "var(--pink-soft)",
            borderLeft: "3px solid var(--pink)",
            boxShadow: "0 2px 10px rgba(23,50,74,.07)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span className="dot" style={{ width: 7, height: 7, background: "var(--pink)" }} />
            <span style={{ font: "700 12.5px var(--mono)", color: "var(--pink-deep)", letterSpacing: ".02em" }}>
              RTD 정지 제안 — 참조(fleet median) 오염 신호 <span className="num">{st.lane1.max_open_run}</span>롤업 연속
              (기준 K=<span className="num">{st.lane1.k}</span>)
            </span>
          </div>
          <div style={{ marginTop: 5, fontSize: 11, color: "var(--faint)" }}>
            <span className="num">{st.lane1.chambers.map((c) => `${c.chamber_id} ${c.open_run}`).join(" · ")}</span>
            {" — 챔버 개별이 아닌 장비 단위 사건 신호입니다. 자동 정지 아님 — 엔지니어 판단 대상."}
            {" (경로② 격리 승격은 2단 배선 대기)"}
          </div>
        </div>
      )}
    </>
  );
}
