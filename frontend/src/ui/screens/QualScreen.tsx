import { useEffect, useState } from "react";
import { qualData } from "../../mock/fdc";
import { APPROVER, fetchLimitVersion, postQualVerdict, postRequalify } from "../../live/api";

/** S7 Qual 판정 — PM 후 5장 고정 표준 조건 (R7) → 4지표 → 조용/요란 분기.
 *  P6-2 실배선: fetch /api/quals/recent → 어댑터 → 렌더. 실패/빈(A5-3 적재 전) = 목업 폴백.
 *
 *  2026-08-10 확정 배선: 하단 버튼이 **화면 이동만** 하고 있었다(`onR9` = openApprovals).
 *  분기 3개도 setBranch 뿐이라 S7 에서는 아무것도 확정되지 않았고, 판정 확정 없이는
 *  가한계(provisional v2)도 PHASE_0 해제도 오지 않는다 — 시나리오 2 의 등뼈가 끊겨 있었다.
 *  이제 2단으로 실호출한다: ① POST /qual/verdict → (폴링이 confirmed 를 잡으면)
 *  ② POST /requalify/{incident_id}. 실패하면 화면을 넘기지 않는다(조용한 실패 금지). */

/** 4지표 1행. `pass: null` = **판정 불참**(제외·미측정) — FAIL 과 구분해야 한다(2026-08-10).
 *  목업(`qualData.metrics`)의 `pass: boolean` 은 이 타입에 그대로 대입된다. */
type QualMetric = { k: string; v: string; limit: string; pass: boolean | null; note?: string };

type QualView = Omit<typeof qualData, "metrics"> & {
  metrics: QualMetric[];
  live?: boolean; confirmed?: string | null; qualId?: string;
  /** R9(ChamberRequalified) 발행 여부 — 게이트웨이 멱등 가드 실황. 로컬 state 가 새로고침에
   *  날아가 완료된 버튼이 되살아나던 문제의 서버측 정답 (2026-08-10). */
  requalified?: boolean;
  /** 확정 본문 조립용 — /quals/recent 파생 컬럼 (2026-08-10). chamberId 는 원형(SIM_CH_4). */
  chamberId?: string; pmCount?: number; incidentId?: string;
};

const fmt = (v: unknown, d = 3) =>
  typeof v === "number" ? v.toFixed(d).replace(/\.?0+$/, "") : "…";
const chOf = (c: unknown) => String(c ?? "").replace(/^SIM_?CH_?/, "CH");

/** quals row(계약 §8) → QualScreen 렌더 형태. wafer별 c65는 quals에 없어 "…"(실값 위장 금지). */
function adaptQual(row: Record<string, unknown>): QualView {
  const t = (row.thresholds as Record<string, unknown>) || {};
  const ae = row.ae_anomaly_mean as number | null;
  const tttm = row.tttm_gap_pct as number | null;
  const nel = (row.nelson_violations as number | null) ?? 0;
  const c65ok = !!row.c65_within_normal;
  const aeLim = Number(t.ae ?? 0.2), tttmLim = Number(t.tttm_pct ?? 1.0);
  // Qual wafer 의 C65 — **예측값**이다. 실측은 원래 없다(Qual 은 fdc.actual 미발행 · WT 미경유,
  // 계약 1-B). 2026-08-10 이전에는 5칸이 전부 "…" 로 비어 관객에게 고장처럼 보였다.
  // 게이트웨이가 `wafer_preds`(wafer_id → predicted_c65)를 동봉하므로 그것을 쓰고, 화면은
  // "예측" 이라고 명시한다 — 실측으로 위장하지 않는다.
  const preds = (row.wafer_preds as Record<string, number> | null) || {};
  const wafers = ((row.wafer_ids as string[]) || []).map((id) => ({
    id, c65: (typeof preds[id] === "number" ? preds[id] : null) as number | null, done: true,
  }));
  const verdict = row.proposed_verdict === "PASS" ? "조용"
    : row.proposed_verdict === "FAIL" ? "요란" : "진행중";
  return {
    chamber: chOf(row.chamber_id),
    pm: "PM 후 Qual", openedAt: row.created_at
      ? new Date(String(row.created_at)).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" })
      : "—",
    phase: row.confirmed_verdict ? 2 : 1,
    wafers: wafers.length ? wafers : qualData.wafers.map((w) => ({ ...w, c65: null })),
    // 🔴 2026-08-10 정정 — 구 렌더는 **NULL 을 FAIL 로 접었다.** 그래서 실제로는 2지표로
    //    내린 판정이 화면에 "4지표 중 4건 FAIL" 로 보였고, "왜 4개 다 실패했나"는 질문에
    //    답이 "2개는 애초에 값이 없다"가 되는 자리였다. 미측정·제외를 판정에서 분리한다.
    //      · AE: `qual.use_ae_axis=false` — PM 직후 AE 가 100% 포화(anomaly 1.0 > 0.2)해
    //        Qual 전건 FAIL 로 수렴하므로 **잠정 제외**(기록만). "레짐이 바뀜"과 "새 레짐이
    //        건전한가"는 다른 질문이라는 판단(2026-08-06 PM 회신). → 제외, FAIL 아님.
    //      · C65 정상범주: Qual wafer 는 fdc.actual 미발행이라 **실측 C65 가 없다** →
    //        `c65_within_normal` 은 NULL 이고 `!!null=false` 가 "이탈"로 렌더됐다. → 미측정.
    //    `pass: null` = 판정 불참. failed 집계와 칩 색이 이걸 구분한다.
    metrics: [
      { k: "AE 재구성 오차", v: ae != null ? fmt(ae) : "—", limit: `< ${aeLim}`,
        pass: (ae != null ? ae <= aeLim : null) as boolean | null,
        note: ae != null ? undefined : "제외 — use_ae_axis=false (PM 직후 포화, 기록만)" },
      { k: "σ-갭 (스냅샷 대비)", v: tttm != null ? `${fmt(tttm, 2)}%` : "—", limit: `< ${tttmLim}%`,
        pass: (tttm != null ? tttm <= tttmLim : null) as boolean | null },
      { k: "Nelson 위반", v: row.nelson_violations != null ? `${nel}건` : "—", limit: "0건",
        pass: (row.nelson_violations != null ? nel === 0 : null) as boolean | null },
      { k: "C65 정상범주", v: row.c65_within_normal != null ? (c65ok ? "정상" : "이탈") : "—",
        limit: String(t.c65 ?? "P95"),
        pass: (row.c65_within_normal != null ? c65ok : null) as boolean | null,
        note: row.c65_within_normal != null ? undefined
              : "미측정 — Qual 은 fdc.actual 미발행(WT 미경유)" },
    ],
    verdict: verdict as QualView["verdict"],
    live: true,
    confirmed: (row.confirmed_verdict as string | null) ?? null,
    requalified: !!row.requalified,
    qualId: String(row.qual_id ?? ""),
    chamberId: String(row.chamber_id ?? ""),
    pmCount: Number(row.pm_count ?? 0),
    incidentId: String(row.incident_id ?? ""),
  };
}

export default function QualScreen({ onR9 }: { onR9: () => void }) {
  // onR9(=openApprovals) 는 호출하지 않는다 (위 2026-08-10 주석 — 자동 이동 제거). prop 은
  // 상위 라우팅 시그니처라 남겨 두고, 미사용 경고만 끈다. 지우려면 App 쪽 호출부도 함께.
  void onR9;
  const [q, setQ] = useState<QualView>(qualData);
  const [branch, setBranch] = useState<"" | "return" | "requal" | "remaint">("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [r9done, setR9done] = useState(false);
  // R9 완료 = 로컬(방금 눌렀다) **또는** 서버 가드(이미 발행됨). 로컬만 보면 새로고침 후
  // 완료된 버튼이 되살아나 재클릭 → 409 였다 (2026-08-10 실측).
  const r9 = r9done || !!q.requalified;

  // P6-2 실배선 — 최신 Qual 조회. 실패/빈 = 목업 유지 (시연 안전망, P5-4 패턴). 5초 폴링.
  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const r = await fetch("/api/quals/recent?n=1");
        if (!r.ok) return;
        const rows = (await r.json())?.quals ?? [];
        if (alive && rows.length) setQ(adaptQual(rows[0]));
      } catch { /* 목업 유지 */ }
    };
    pull();
    const t = setInterval(pull, 5000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  // ── 해제 근거 Brief (RLS · #154/#167) — R9 를 누르는 사람이 읽는 근거 ──────────
  //   GET /requalify/{incident_id}/package 의 release_brief 절. null = 이 Incident 에
  //   RTD 정지가 없었다(정상) — 카드 자체를 비운다(#167 계약: 화면은 그 절만 비운다).
  //   생성이 정지 후 약 1분(폴러 30s + LLM 작성 30s)이라 잡힐 때까지 10s 폴링, 잡히면 멈춘다.
  const [rls, setRls] = useState<any | null>(null);
  useEffect(() => {
    setRls(null);
    if (!q.incidentId) return;
    let alive = true;
    const t = window.setInterval(() => pull(), 10000);
    const pull = async () => {
      try {
        const r = await fetch(`/api/requalify/${q.incidentId}/package`);
        if (!r.ok) return;
        const b = (await r.json())?.release_brief ?? null;
        if (alive && b) { setRls(b); window.clearInterval(t); }
      } catch { /* 절만 비움 — 조용한 폴백이 계약. Brief 부재 ≠ 오류 */ }
    };
    pull();
    return () => { alive = false; window.clearInterval(t); };
  }, [q.incidentId]);

  // 판정 근거는 **값이 있는 지표만**이다 (pass===null = 미측정·제외 → 집계 제외).
  const judged = q.metrics.filter((m) => m.pass !== null && m.pass !== undefined);
  const failed = judged.filter((m) => m.pass === false).length;
  const skipped = q.metrics.length - judged.length;
  const decided = !!q.confirmed;
  const canConfirm = !!q.qualId && !!q.chamberId
    && (q.verdict !== "요란" || !!q.incidentId);

  // ── 2단 실행 — ① 판정 확정 → ② R9 재적격. 실패 시 화면 이동 없음. ──────────
  const runAction = async () => {
    setBusy(true);
    try {
      if (!decided) {
        const g = await postQualVerdict({
          qual_id: q.qualId ?? "", chamber_id: q.chamberId ?? "",
          verdict: q.verdict === "요란" ? "loud" : "quiet",
          pm_count: q.pmCount ?? 0, approved_by: APPROVER.approver,
          incident_id: q.incidentId || undefined,
        });
        setNote(g.detail);
        // 🔴 2026-08-10 — Approvals 자동 이동 **제거**. 판정 확정은 이벤트 발행으로 끝난다:
        //   gateway `/qual/verdict` 는 QualVerdictConfirmed 를 emit 만 하고(quals 는 B 가 닫는다),
        //   approval_records 에 카드를 만들지 않는다. 그래서 넘어간 Approvals 에는 이 판정에
        //   해당하는 카드가 없고, 목록이 목업 카드를 먼저 선택해 **엉뚱한 Incident 창**이 떴다
        //   (리허설 실측 2회). 확정 결과는 이 자리에 note 로 남고, 5초 폴링이 confirmed 를
        //   잡으면 같은 버튼이 [R9 재적격 실행]으로 바뀐다 — 체인 ①→② 가 한 화면에서 이어진다.
        return;
      }
      // limit_version 은 현행 active 에서 읽는다 — 빈 값이면 보내지 않는다.
      const lv = await fetchLimitVersion(q.chamberId ?? "");
      if (!lv) { setNote("limit_version 미확인 — R9 보류 (빈 본문 전송 금지)"); return; }
      const g = await postRequalify(q.incidentId ?? "", {
        chamber_id: q.chamberId ?? "", qual_id: q.qualId ?? "",
        pm_count: q.pmCount ?? 0, corrections_applied: [],
        limit_version: lv, approved_by: APPROVER.approver,
      });
      setNote(g.detail);
      // R9 는 화면을 넘기지 않는다. ① 승인 요청은 Approvals 로 가는 게 맞지만 R9 는 그
      // 승인의 **하류**라 넘길 곳이 없고, 넘기면 r9done(로컬)이 리셋돼 버튼이 다시
      // "R9 재적격 실행"으로 보인다 → 다시 누르면 멱등 409. 결과를 이 자리에서 보여준다.
      if (g.live) setR9done(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="scr">
      <div className="scrh">
        <div>
          <div className="t">
            Qual
            {q.live && <span className="lgi" style={{ marginLeft: 8, color: "var(--mint-deep)" }}>● LIVE</span>}
          </div>
          <div className="c num">{q.chamber} · {q.pm} · 개방 {q.openedAt} — 모든 개방 후 Qual 5장 (고정 표준 조건 R7)</div>
        </div>
        <div className="phasebar num">
          {["Phase 0", "Phase 1", "Phase 2"].map((p, i) => (
            <span key={p} className={"ph" + (i === q.phase ? " on" : i < q.phase ? " done" : "")}>{p}</span>
          ))}
        </div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <div className="col">
          <div className="card">
            <div className="chead"><span className="t">Qual Wafer 진행</span><span className="cap num">{q.wafers.filter(w => w.done).length}/5 계측</span></div>
            <div className="qwrow">
              {q.wafers.map((w) => (
                <div key={w.id} className={"qwaf" + (w.done ? " done" : "")}>
                  <div className="num" style={{ fontSize: 9, color: "var(--faint)" }}>{w.id}</div>
                  <div className="num" style={{ fontWeight: 700, fontSize: 14, color: w.done ? "var(--navy)" : "var(--faint)" }}>
                    {w.c65 != null ? w.c65.toLocaleString() : "…"}
                  </div>
                </div>
              ))}
            </div>
            <div className="scrfoot num" style={{ padding: "0 16px 12px" }}>
              숫자는 <b>C65 예측</b> (A 파이프라인) — Qual wafer는 fdc.actual 미발행이라 실측이 없다 (계약 v4.6 · WT 미경유) · 알람 억제 = Phase 0
            </div>
          </div>

          <div className="card">
            <div className="chead">
              <span className="t">4지표 판정</span>
              <span className="cap num" title="AE = qual.use_ae_axis=false 로 잠정 제외(PM 직후 포화·기록만) · C65 정상범주 = Qual 은 fdc.actual 미발행이라 실측이 없다. 둘은 판정에 불참한다">
                A5-3 취합 · 판정 {judged.length}지표 중 <b>{failed}건 FAIL</b>
                {skipped > 0 ? ` · 불참 ${skipped}(제외·미측정)` : ""}
              </span>
            </div>
            {q.metrics.map((m) => {
              const na = m.pass === null || m.pass === undefined;   // 미측정·제외 = 판정 불참
              return (
              <div className="krow" key={m.k} title={m.note}>
                <b style={{ width: 150, color: na ? "var(--label)" : undefined }}>{m.k}</b>
                <span className="num" style={{ fontWeight: 700,
                      color: na ? "var(--faint)" : m.pass ? "var(--mint-deep)" : "var(--pink)" }}>{m.v}</span>
                <span className="num" style={{ color: "var(--faint)", fontSize: 10.5 }}>기준 {m.limit}</span>
                {m.note && (
                  <span className="num" style={{ fontSize: 10, color: "var(--faint)",
                        maxWidth: 210, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {m.note}
                  </span>
                )}
                <span className={"schip " + (na ? "" : m.pass ? "ok" : "warn")}
                      style={{ marginLeft: "auto",
                               ...(na ? { background: "var(--hairline)", color: "var(--label)" }
                                      : m.pass ? {} : { background: "var(--pink-soft)", color: "var(--pink-deep)" }) }}>
                  {na ? "판정 불참" : m.pass ? "PASS" : "FAIL"}
                </span>
              </div>
              );
            })}
          </div>
        </div>

        <div className="col">
          <div className="card">
            <div className="chead">
              <span className="t">판정 — {q.verdict === "요란" ? "요란 (레짐 전환)" : q.verdict}</span>
              {/* 구 문구는 verdict 확정을 곧 "R9 완료"로 적었다 — 둘은 다른 단계다
                  (확정 = QualVerdictConfirmed / R9 = ChamberRequalified). 분리해 표기. */}
              <span className="cap num">
                {r9 ? `확정: ${q.confirmed} · R9 발행됨`
                  : decided ? `확정: ${q.confirmed} — R9 대기`
                  : q.verdict === "요란" ? "REGIME TRANSITION INCIDENT 개설 (신규 결정 10)" : ""}
              </span>
            </div>
            <div className="vreason" style={{ padding: "0 16px" }}>
              판정 {judged.length}지표 중 {failed}건 FAIL → <b>{q.verdict} 판정</b>. 가한계 Phase 진입, A는 is_post_loud_pm 리셋·'추정' bias(프라이어), 정착 1,000장 후 Phase 2 재산정.
            </div>
            <div className="branch3">
              {([
                ["return", "복귀", "조용 수준 회복 시 — bias·라벨 연속"],
                ["requal", "추가 Qual", "판정 불확실 — 5장 재계측"],
                ["remaint", "재정비", "지표 심각 이탈 — 정비 재개방"],
              ] as const).map(([k, t, d]) => (
                <button key={k} className={"bopt" + (branch === k ? " on" : "")}
                        disabled={decided} onClick={() => setBranch(k)}>
                  <b>{t}</b><span>{d}</span>
                </button>
              ))}
            </div>
            <div style={{ padding: "4px 16px 14px" }}>
              <button className="gopill"
                      style={{ opacity: (branch || decided) && !busy && !r9 ? 1 : .45 }}
                      disabled={(!branch && !decided) || busy || r9 || !canConfirm}
                      title={!canConfirm
                        ? "확정 본문이 아직 다 안 모였습니다 — 요란 확정은 레짐 전환 Incident 가 필요합니다"
                        : decided ? "POST /requalify — 신규 기준선 수립 (멱등: Incident 당 1회)"
                                  : "POST /qual/verdict — 판정 확정 → 가한계 Phase 진입"}
                      onClick={runAction}>
                {/* 「→ Approvals」 꼬리표 제거 (2026-08-10) — 이 버튼은 화면을 넘기지 않는다.
                    확정도 R9 도 여기서 끝나고 결과는 아래 note 에 남는다. */}
                {r9 ? "R9 확정 완료"
                  : busy ? "실행 중…"
                  : decided ? "R9 재적격 실행 (신규 기준선)"
                  : branch === "return" ? "복귀 승인 요청" : branch === "requal" ? "추가 Qual 실행"
                  : branch === "remaint" ? "재정비 요청" : "분기 선택"}
              </button>
              {/* 보낼 본문을 미리 보여준다 — 빈 값이면 누르기 전에 눈으로 걸린다.
                  (2026-08-09 빈 pm_count 로 422 를 받은 사고의 재발 방지) */}
              {q.live && (
                <div className="num" style={{ marginTop: 6, fontSize: 10, color: "var(--faint)" }}>
                  {q.qualId || "qual_id 없음"} · {q.incidentId || "Incident 대기"} · pm_count {q.pmCount ?? 0}
                </div>
              )}
              {note && (
                <div className="num" style={{ marginTop: 4, fontSize: 10.5, color: "var(--label)" }}>{note}</div>
              )}
            </div>
            <div className="scrfoot num" style={{ padding: "0 16px 12px" }}>
              순차 승인 체인 (헌법 1-4 P3): 분기 승인 → R9 신규 기준선 수립(A2·A3 상한 미적용) → CT②(AE) 재학습 승인
            </div>
          </div>

          {rls && (
            <div className="card">
              <div className="chead">
                <span className="t">해제 근거 Brief — 재가동 판단 재료</span>
                <span className="cap num">
                  {String(rls.report_id ?? "")}
                  {" · "}{String(rls.readiness ?? "").toUpperCase() || "판단 유보"}
                  {typeof rls.confidence === "number" ? ` · conf ${rls.confidence}` : ""}
                </span>
              </div>
              {rls.summary && (
                <div className="vreason" style={{ padding: "0 16px" }}>{String(rls.summary)}</div>
              )}
              {Array.isArray(rls.precheck_items) && rls.precheck_items.length > 0 && (
                <div style={{ padding: "6px 16px 2px" }}>
                  {rls.precheck_items.slice(0, 4).map((p: any, i: number) => (
                    <div className="krow" key={i}>
                      <b>사전 확인</b>
                      <span style={{ marginLeft: "auto", textAlign: "right" }}>{String(p)}</span>
                    </div>
                  ))}
                </div>
              )}
              {rls.counter_evidence != null && String(rls.counter_evidence) !== "" && (
                <div className="num" style={{ padding: "4px 16px 10px", fontSize: 10.5, color: "var(--label)" }}>
                  반증: {Array.isArray(rls.counter_evidence)
                    ? rls.counter_evidence.map(String).join(" · ") : String(rls.counter_evidence)}
                </div>
              )}
              <div className="scrfoot num" style={{ padding: "0 16px 12px" }}>
                readiness 는 권고값이다 — 해제는 이 화면의 R9 승인만이 한다 (헌법 1-1 예외 4 ⓒ)
              </div>
            </div>
          )}

          <div className="card">
            <div className="chead"><span className="t">레짐 전환 진행 (F8)</span></div>
            {/* 2026-08-10 정정: ②가 `decided`(=verdict 확정)에 걸려 있어서 판정만 확정해도
                "R9 완료"로 표시됐다 — 실제 R9(ChamberRequalified)는 아직 안 나간 상태였다.
                ①은 화면을 떠났다 돌아오면 branch 로컬 상태가 리셋돼 확정 후에도 "대기"로
                되돌아갔다. ① = 확정 여부(서버 상태), ② = R9 발행 여부로 각각 다시 묶는다. */}
            {[
              ["① 분기 승인", decided ? "승인됨" : branch ? "선택됨" : "대기", decided || !!branch],
              ["② R9 신규 기준선 수립", r9 ? "완료" : decided ? "대기 (확정 완료)" : "대기", r9],
              ["③ CT② AE 재학습", "대기 (Phase 2 예약 — R6)", false],
            ].map(([t, s, on]) => (
              <div className="krow" key={t as string}>
                <b>{t}</b>
                <span className="num" style={{ marginLeft: "auto", color: on ? "var(--mint-deep)" : "var(--faint)" }}>{s}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
