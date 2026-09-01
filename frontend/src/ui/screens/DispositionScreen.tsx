import { useEffect, useMemo, useState } from "react";
import { dispositions, varStats } from "../../mock/fdc";
import { requestDisposition } from "../../live/api";

/** S6 Disposition — HOLD wafer 처분 (실행은 승인 게이트 경유 — 헌법 1-1).
 *  P6-2 실배선: fetch /api/dispositions/pending → per_wafer 장별 렌더(계약 §4-B 상태 전이).
 *  pred/conf는 wafer_dispositions 컬럼 부재 → recommendation_basis 텍스트 인용에서 파싱
 *  (계약 §6 각주 — 없으면 "—", 실값 위장 금지). 실패/빈 = 목업 폴백(P5-4 패턴). */

type Row = {
  wafer: string; lot: string; chamber: string; incident: string;
  rec: string; reason: string; pred: number | null; conf: number | null;
  status?: string; decided?: boolean;
};

const chOf = (c: unknown) => String(c ?? "").replace(/^SIM_?CH_?/, "CH");
/** basis 텍스트에서 predicted_c65 숫자 추출 (계약 §6 인용 규약). 없으면 null. */
const parsePred = (basis: string | null): number | null => {
  const m = (basis || "").match(/predicted_c65[^\d-]*([\d,]+(?:\.\d+)?)/i);
  return m ? Number(m[1].replace(/,/g, "")) : null;
};

/** wafer_dispositions row → S6 렌더 형태. */
function adaptDispo(r: Record<string, unknown>): Row {
  const basis = (r.recommendation_basis as string | null) || (r.hold_reason as string | null) || "";
  return {
    wafer: String(r.wafer_id ?? ""), lot: String(r.lot_id ?? "—"),
    chamber: chOf(r.chamber_id), incident: String(r.incident_id ?? ""),
    rec: String(r.system_recommendation ?? r.status ?? "HOLD"),
    reason: basis, pred: parsePred(basis), conf: null,
    status: String(r.status ?? "HOLD"), decided: r.decided_by != null,
  };
}

export default function DispositionScreen({ onApprove }: { onApprove: (incidentId: string) => void }) {
  const [rows, setRows] = useState<Row[]>(dispositions as Row[]);
  const [live, setLive] = useState(false);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const toggle = (w: string) => {
    const s = new Set(sel); s.has(w) ? s.delete(w) : s.add(w); setSel(s);
  };

  // P6-2 실배선 — 처분 목록 5초 폴링. 실패/빈 = 목업 유지 (시연 안전망).
  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const r = await fetch("/api/dispositions/pending?n=100");
        if (!r.ok) return;
        const raw = (await r.json())?.dispositions ?? [];
        if (alive && raw.length) {
          const next: Row[] = raw.map(adaptDispo);
          setRows(next); setLive(true);
          // 목록에서 밀린(LIMIT 100) 선택은 정리한다 — 안 그러면 `선택 N건` 표시가 실제
          // 전송 대상과 어긋난다 (C 소비자 리뷰 #67 ②). 동일하면 참조 유지(재렌더 억제).
          setSel((prev) => {
            const keep = new Set([...prev].filter((w) => next.some((d) => d.wafer === w)));
            return keep.size === prev.size ? prev : keep;
          });
        }
      } catch { /* 목업 유지 */ }
    };
    pull();
    const t = setInterval(pull, 5000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  // 상단 집계는 **잠정(미확정) 행만** 센다 — approval_graph가 같은 행을 UPDATE하므로(행 삭제 없음)
  // 전체를 세면 승인해도 숫자가 줄지 않는다 (C 소비자 리뷰 #60 ⓑ 실측). 목업 모드는 전 행 잠정 취급.
  const isProvisional = (d: Row) => !live || (d.status === "HOLD" && !d.decided);
  const openRows = rows.filter(isProvisional);
  const scrap = openRows.filter((d) => d.rec === "SCRAP").length;
  const decidedN = rows.length - openRows.length;
  // 선택 → Incident 해석 (C 소비자 리뷰 #67 ①·③).
  //  ① 폴백은 **잠정 행**만 — 구 `rows[0]`은 확정 행일 수 있어 엉뚱한 Incident로 승인이 나가고,
  //     PENDING 없는 incident에 POST /approvals는 200을 주면서 approval_records엔 안 남는다(조용한 실패).
  //  ③ 선택이 여러 Incident에 걸치면 전송을 막는다 — 구현은 첫 Incident 1건만 보내는데 버튼은
  //     `선택 N건`이라 나머지가 조용히 누락됐다. 헌법 1-4(Incident당 동시 pending 1건)와도 정합.
  const selRows = useMemo(() => rows.filter((d) => sel.has(d.wafer)), [sel, rows]);
  const selIncidents = useMemo(
    () => Array.from(new Set(selRows.map((d) => d.incident))), [selRows]);
  const multiIncident = selIncidents.length > 1;
  const selIncident = selIncidents[0] ?? openRows[0]?.incident ?? "";
  const canApprove = selRows.length > 0 && !multiIncident && !!selIncident;

  // ── 처분 승인 요청 3종 재건 (2026-08-12 — revert 유실분. S4 주석 "[RELEASE로 요청]·
  //    [SCRAP으로 요청]·[권고대로 요청]" 이 참조하는 그 버튼들) ──────────────────────
  //  · 권고대로  = wafer_ids 만 전송 → 에이전트 권고 프리필 (8/11 PM 결정의 정본 경로)
  //  · 전체 RELEASE / 전체 SCRAP = decisions[] 로 판정까지 실어 전송 (api.ts §처분 참조)
  //  · HOLD(보류)는 요청에 싣지 않는다 — S4 승인 화면의 장별 토글에서 보류한다 (계약 §6:
  //    보류 장은 잠정 유지 · API DispositionDecision 타입도 RELEASE|SCRAP 만 — 설계 정합)
  //  요청 성공 → LLM 처분 Brief 생성(수 초) → S4 처분 전용 카드 개설 → onApprove 로 이동
  //  (S4 는 처분 카드 우선 점프가 이미 구현돼 있다). 실패는 detail 그대로 노출(실값 위장 금지).
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const submit = async (mode: "rec" | "RELEASE" | "SCRAP") => {
    if (!canApprove || busy) return;
    setBusy(true); setNote("처분 Brief 생성 중… (LLM 수 초 소요)");
    const ids = selRows.map((d) => d.wafer);
    const res = mode === "rec"
      ? await requestDisposition(selIncident, ids)
      : await requestDisposition(selIncident, ids,
          ids.map((w) => ({ wafer_id: w, disposition: mode })));
    setBusy(false); setNote(res.detail);
    if (res.live) { setSel(new Set()); onApprove(selIncident); }
  };

  return (
    <div className="scr">
      <div className="scrh">
        <div>
          <div className="t">
            Disposition
            {live && <span className="lgi" style={{ marginLeft: 8, color: "var(--mint-deep)" }}>● LIVE</span>}
          </div>
          <div className="c num">
            HOLD {openRows.length} · SCRAP 후보 {scrap}
            {decidedN ? ` · 확정 ${decidedN}` : ""}
            {live ? "" : ` · VALUE AT RISK ${varStats.amount}`}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button
            className="gopill"
            style={{ opacity: canApprove && !busy ? 1 : .45 }}
            disabled={!canApprove || busy}
            onClick={() => submit("rec")}
            title={multiIncident
              ? `Incident ${selIncidents.length}건이 함께 선택됨 — 하나씩 요청하세요 (헌법 1-4)`
              : "에이전트 권고 프리필로 처분 카드 개설 — 실행은 승인 게이트(S4) 경유 (헌법 1-1)"}
          >
            {multiIncident
              ? `Incident ${selIncidents.length}건 — 하나만 선택`
              : busy ? "요청 중…" : `권고대로 요청 (${selRows.length}장) →`}
          </button>
          <button
            className="gopill"
            style={{ opacity: canApprove && !busy ? 1 : .45, background: "var(--mint-soft, #e3f6ef)", color: "var(--mint-deep, #0d7a5f)" }}
            disabled={!canApprove || busy}
            onClick={() => submit("RELEASE")}
            title="선택 전체를 RELEASE 판정으로 실어 카드 개설 — 최종 확정은 S4 (장별 수정·보류 가능)"
          >전체 RELEASE로 요청</button>
          <button
            className="gopill"
            style={{ opacity: canApprove && !busy ? 1 : .45, background: "var(--pink-soft, #fde8ef)", color: "var(--pink-deep, #b0175a)" }}
            disabled={!canApprove || busy}
            onClick={() => submit("SCRAP")}
            title="선택 전체를 SCRAP 판정으로 실어 카드 개설 — 최종 확정은 S4 (장별 수정·보류 가능)"
          >전체 SCRAP으로 요청</button>
        </div>
      </div>

      {note && (
        <div className="c num" style={{ margin: "2px 0 8px", fontSize: 11.5,
              color: note.includes("실패") || note.includes("무응답") || note.includes("없") ? "var(--alarm-deep, #c23f38)" : "var(--label)" }}>
          {note}
        </div>
      )}

      <div className="card">
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ width: 34 }} />
              <th>WAFER</th><th>LOT</th><th>CH</th>
              <th className="num">C65 예측</th><th className="num">CONF</th>
              <th>권고</th><th>근거</th><th>INCIDENT</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((d) => {
              // 잠정(미확정) = decided_at IS NULL — 계약 §4-B 표시 규약 (C 소비자 리뷰 확인 2).
              // 잠정 행의 rec은 "STEP 4 후보 마커"지 판정이 아님 → 후보 표기(amber)로 렌더.
              const prov = live && isProvisional(d);
              return (
              <tr key={d.wafer + d.incident} className={sel.has(d.wafer) ? "on" : ""} onClick={() => toggle(d.wafer)}>
                <td><span className={"cb" + (sel.has(d.wafer) ? " on" : "")} /></td>
                <td className="num">{d.wafer}</td>
                <td className="num">{d.lot}</td>
                <td className="num">{d.chamber}</td>
                <td className="num" style={{ fontWeight: 700, color: d.rec === "SCRAP" && !prov ? "var(--pink)" : "var(--text)" }}>
                  {d.pred != null ? d.pred.toLocaleString() : "—"}
                </td>
                <td className="num" style={{ color: d.conf != null && d.conf < 0.8 ? "var(--alarm-deep)" : "var(--label)" }}>
                  {d.conf != null ? d.conf.toFixed(2) : "—"}
                </td>
                <td>
                  <span className={"schip " + (d.rec === "SCRAP" && !prov ? "warn" : "ok")}
                        title={prov ? "B9 자동 격리 후보 — 최종 판정은 승인 시 확정 (계약 §4-B)" : undefined}
                        style={prov ? { background: "var(--amber-soft, #fef3e0)", color: "var(--amber-deep, #b8720f)" }
                                    : d.rec === "SCRAP" ? { background: "var(--pink-soft)", color: "var(--pink-deep)" } : undefined}>
                    {prov ? d.rec + " 후보" : d.rec}
                  </span>
                  {prov &&
                    <span className="schip" style={{ marginLeft: 4, background: "var(--amber-soft, #fef3e0)", color: "var(--amber-deep, #b8720f)", fontSize: 9 }}>잠정</span>}
                </td>
                <td style={{ fontSize: 11.5, color: "var(--label)" }}>{d.reason}</td>
                <td className="num" style={{ fontSize: 10, color: "var(--faint)" }}>{d.incident.replace("INC-2026", "")}</td>
              </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="scrfoot num">권고 = C 통합 Agent (C6-1) · 확정 = wafer_dispositions (승인 후) · 잠정 HOLD = B9 자동 격리(승인 전) · 예측 CONF&lt;0.8 = LOW 플래그 우선 실검사</div>
    </div>
  );
}
