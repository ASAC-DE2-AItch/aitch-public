import { useEffect, useMemo, useState } from "react";
import type { LiveChamberView } from "../live/useLiveFdc";
import { yOf, X_MAX, watchOf, SENSOR_KEYS, type SensorKey } from "../live/useLiveFdc";

const W = 560;
const H = 190;

// x축 위치 = 0σ 기준선 (σ↔y 단일 매핑에서 파생) — viewBox 바닥(≈−0.5σ)에 두면 거짓 원점이 됨
const AXIS_Y = yOf(0);
// 차트 블록(0~X_MAX)을 카드 중앙에 — 남는 여백을 좌우로 균등 분배
const PAD_L = (W - X_MAX) / 2;

// F2 마커 색 — 승인(관리선 갱신) 이벤트. 위반(핑크)·측정(파랑)과 구분되는 앰버
const MARKER = "#e8a33d";

function smoothPath(p: [number, number][]): string {
  if (p.length < 2) return "";
  let d = `M ${p[0][0]},${p[0][1]}`;
  for (let i = 0; i < p.length - 1; i++) {
    const a = p[i - 1] ?? p[i];
    const b = p[i];
    const c = p[i + 1];
    const e = p[i + 2] ?? c;
    d += ` C ${b[0] + (c[0] - a[0]) / 6},${b[1] + (c[1] - a[1]) / 6}`
      + ` ${c[0] - (e[0] - b[0]) / 6},${c[1] - (e[1] - b[1]) / 6}`
      + ` ${c[0]},${c[1]}`;
  }
  return d;
}

const halo = { paintOrder: "stroke" as const, stroke: "var(--card)", strokeWidth: 3 };

/** AURA 플로우 문법 SPC 차트 — 주시 센서 기본 + 센서 칩 전환.
 *  파랑=측정 σ(append-only, 과거 동결), 핑크 점선=UCL +3σ, 핑크 해치=Nelson 위반 구간.
 *  W6-①: raw 실피드 센서는 RAW 배지 + F3 관리선 버전 칩 + F2 승인 마커(앰버 수직선). */
export default function SpcFlow({ v, onExpand }: { v: LiveChamberView; onExpand?: () => void }) {
  const ch = v.ch;
  const watch = watchOf(ch);
  const [sel, setSel] = useState<SensorKey>(watch);
  // 챔버 전환 시 주시 센서로 리셋 (알람 유발 센서가 기본)
  useEffect(() => { setSel(watchOf(ch)); }, [ch.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const data = v.charts.find((c) => c.sensor === sel) ?? v.charts[0];
  const spcSuffix = ch.spcLabel.split(" · ").slice(1).join(" · "); // STEP·윈도우 부분

  const { bl, blArea, head } = useMemo(() => {
    const line = smoothPath(data.points);
    const first = data.points[0];
    const last = data.points[data.points.length - 1];
    return {
      bl: line,
      blArea: `${line} L ${last[0]},${AXIS_Y} L ${first[0]},${AXIS_Y} Z`,
      head: last,
    };
  }, [data.points]);

  // LIVE 툴팁 = 2줄 가운데 정렬 — σ(주) 위, 실측값(번역) 아래 (UCL 라벨 순서와 통일)
  // 전 챔버 공통 (2026-07-20) — 활성 챔버(headSig 有)는 모두 헤드 대화상자 표시
  const peak = data.headSig != null
    ? {
        x: head[0], y: head[1],
        sig: `${data.headSig >= 0 ? "+" : ""}${data.headSig.toFixed(1)}σ`,
        eng: data.headEng ?? "—",
      }
    : undefined;

  // 관리선 y — σ 매핑에서 파생 (곡선과 같은 자로 잰다). W6-①: 양측(UCL·LCL) 표시
  const uclY = yOf(3);
  const lclY = yOf(-3);

  return (
    <div className="card flow">
      <div className="fhead">
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <div className="fbig num">{data.headSig == null ? "—" : data.headSig.toFixed(1)}<span> σ</span></div>
            {/* 센서 전환 — 위반 진행 중인 센서엔 핑크 점 */}
            <div className="ssel num">
              {SENSOR_KEYS.map((s) => {
                const c = v.charts.find((x) => x.sensor === s);
                // 센서 상태 점: 위반 진행(crit·blink) / 경고 2σ+(warn) / 정상(무채색 — ISA)
                const st = (c?.nelsonBands.some((b) => b.open))
                  ? "crit"
                  : (c?.headSig != null && c.headSig >= 2 ? "warn" : "ok");
                return (
                  <button
                    key={s}
                    className={"sbtn" + (sel === s ? " on" : "")}
                    onClick={() => setSel(s)}
                    title={c?.isWatch ? `${s} — 주시 센서 (알람 유발)` : s}
                  >
                    <i className={"sd " + st} />
                    {s}
                  </button>
                );
              })}
            </div>
          </div>
          <div className="fdelta" style={ch.sev === "critical" && sel === watch ? undefined : { color: "var(--label)" }}>
            {data.delta}
          </div>
        </div>
        <div className="flegend" style={onExpand ? { paddingRight: 40 } : undefined}>
          <span className="lgi"><i className="swf" />MEASURED σ</span>
          <span className="lgi"><i className="swd" />UCL +3σ</span>
          <span className="lgi"><i className="sww" />NELSON WINDOW</span>
          {data.real && (
            <span className="lgi" style={{ color: MARKER }} title="fdc.raw 실데이터 곡선 (settled 평균 · 현행 관리선 σ)">
              ● RAW
            </span>
          )}
        </div>
        {onExpand && (
          <button className="ex exabs" aria-label="센서 상세 (S2)" title="센서 상세 — SPC Charts" onClick={onExpand}>
            <svg className="ic" viewBox="0 0 24 24" style={{ width: 14, height: 14 }}>
              <path d="M15 4h5v5M9 20H4v-5M20 4l-6 6M4 20l6-6" />
            </svg>
          </button>
        )}
      </div>

      <div className="fchart">
        {peak && (
          <div
            className="ftt num"
            style={{
              left: `min(${(((peak.x + PAD_L) / W) * 100).toFixed(2)}%, calc(100% - 42px))`,
              top: `max(2px, calc(5px + ${(peak.y / H).toFixed(4)} * (100% - 15px) - 50px))`,
            }}
          >
            <div>{peak.sig}</div>
            <div className="sub">{peak.eng}</div>
          </div>
        )}
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
          <defs>
            <pattern id="hB" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
              <line x1="0" y1="0" x2="0" y2="6" stroke="var(--trace)" strokeWidth="1.1" opacity=".3" />
            </pattern>
            <pattern id="hA" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
              <line x1="0" y1="0" x2="0" y2="6" stroke="var(--pink)" strokeWidth="1.3" opacity=".45" />
            </pattern>
          </defs>

          <g transform={`translate(${PAD_L},0)`}>
          {data.nelsonBands.map((b) => (
            <g key={b.x1}>
              <rect x={b.x1} y="0" width={b.x2 - b.x1} height={H} fill="rgba(255,99,146,.07)" />
              <rect x={b.x1} y="0" width={b.x2 - b.x1} height={H} fill="url(#hA)" opacity={b.open ? 1 : 0.55} />
              <line x1={b.x1} y1="0" x2={b.x1} y2={H} stroke="var(--pink)" strokeWidth="1" strokeDasharray="4 4" opacity=".55" />
              {!b.open && (
                <line x1={b.x2} y1="0" x2={b.x2} y2={H} stroke="var(--pink)" strokeWidth="1" strokeDasharray="4 4" opacity=".55" />
              )}
            </g>
          ))}
          <path d={blArea} fill="url(#hB)" />

          {/* x축(wafer 순번) = 0σ 기준선 · y축(σ) = 좌측, 차트 바닥(≈−0.5σ)까지 */}
          <line x1="0.5" y1="12" x2="0.5" y2={H - 1} stroke="var(--axis)" strokeWidth="1" />
          <line x1="0" y1={AXIS_Y} x2={X_MAX} y2={AXIS_Y} stroke="var(--axis)" strokeWidth="1" />
          <text x="5" y={AXIS_Y + 11} fill="var(--faint)" fontSize="8.5" fontFamily="monospace" style={halo}>0σ</text>
          <text x={X_MAX} y={AXIS_Y + 11} textAnchor="end" fill="var(--faint)" fontSize="8.5" fontFamily="monospace" style={halo}>WAFER →</text>

          <line x1="0" y1={uclY} x2={X_MAX} y2={uclY} stroke="var(--pink)" strokeWidth="1.6" strokeDasharray="6 5" />
          <path d={bl} fill="none" stroke="var(--trace)" strokeWidth="2.6" strokeLinecap="round" />
          <text x="8" y={uclY - 5} fill="var(--pink)" fontSize="9" fontWeight="600" fontFamily="monospace" style={halo}>UCL +3σ</text>
          {/* UCL 선 바로 아래 — 공학단위 임계치 */}
          {data.uclEng && (
            <text x="8" y={uclY + 13} fill="var(--pink)" fontSize="8.5" fontWeight="600" fontFamily="monospace" style={halo}>
              {data.uclEng}
            </text>
          )}
          {/* LCL −3σ — 관리선은 양측 (W6-①: 음수 σ 실데이터 대응) */}
          <line x1="0" y1={lclY} x2={X_MAX} y2={lclY} stroke="var(--pink)" strokeWidth="1.2" strokeDasharray="6 5" opacity=".75" />
          <text x="8" y={lclY + 12} fill="var(--pink)" fontSize="9" fontWeight="600" fontFamily="monospace" style={halo}>LCL -3σ</text>

          {/* F2 — 승인(관리선 갱신) 마커: 승인 순간의 wafer 위치에 앰버 수직선 + 버전 라벨 */}
          {(data.markers ?? []).map((m) => (
            <g key={`${m.x}-${m.label}`}>
              <line x1={m.x} y1="14" x2={m.x} y2={H - 2} stroke={MARKER} strokeWidth="1.6" strokeDasharray="2 3" />
              <text x={m.x + 4} y="22" fill={MARKER} fontSize="8.5" fontWeight="600" fontFamily="monospace" style={halo}>
                LIMIT {m.label}
              </text>
            </g>
          ))}

          {peak ? (
            <circle cx={peak.x} cy={peak.y} r="5" fill="var(--card)" stroke="var(--trace)" strokeWidth="3" />
          ) : (
            data.headSig != null && (
              <circle cx={head[0]} cy={head[1]} r="3" fill="var(--card)" stroke="var(--trace)" strokeWidth="2" />
            )
          )}
          </g>
        </svg>
      </div>
      <div className="ftarget num" style={{ textAlign: "right", padding: "0 18px 12px" }}>
        {sel} · {spcSuffix} · LIMIT {data.limitVer ?? ch.limitVer}{data.real ? " · RAW LIVE" : ""}
      </div>
    </div>
  );
}
