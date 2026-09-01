// AURA 에어리어 차트 클론 — 소프트 필 + 라인 + 대시 임계선 + 엔드 도트
export default function AreaTrend({ data, ucl, w = 520, h = 150, stroke = "var(--accent)" }: {
  data: number[]; ucl?: number; w?: number; h?: number; stroke?: string;
}) {
  if (data.length < 2) return null;
  const min = Math.min(...data, ucl ?? Infinity) * 0.985;
  const max = Math.max(...data, ucl ?? -Infinity) * 1.015;
  const rng = max - min || 1;
  const px = (i: number) => (i / (data.length - 1)) * (w - 8) + 4;
  const py = (v: number) => h - 6 - ((v - min) / rng) * (h - 16);
  const pts = data.map((v, i) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`);
  const line = pts.join(" ");
  const area = `4,${h - 6} ${line} ${w - 4},${h - 6}`;
  const gid = `at-${Math.round(w)}-${Math.round(h)}`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="block w-full" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={stroke} stopOpacity="0.18" />
          <stop offset="1" stopColor={stroke} stopOpacity="0.02" />
        </linearGradient>
      </defs>
      {ucl !== undefined && (
        <line x1={4} y1={py(ucl)} x2={w - 4} y2={py(ucl)}
          stroke="var(--crit)" strokeOpacity={0.55} strokeWidth={1.2} strokeDasharray="5 5" />
      )}
      <polygon points={area} fill={`url(#${gid})`} />
      <polyline points={line} fill="none" stroke={stroke} strokeWidth={2} strokeLinejoin="round" />
      <circle cx={px(data.length - 1)} cy={py(data[data.length - 1])} r={3.5} fill={stroke}
        stroke="var(--widget)" strokeWidth={1.5} />
    </svg>
  );
}
