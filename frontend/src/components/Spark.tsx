// 인라인 스파크라인 — 계측 밀도의 기본 단위 (박스 없음)
export default function Spark({ data, w = 96, h = 22, stroke = "var(--text-faint)", dot = true }: {
  data: number[]; w?: number; h?: number; stroke?: string; dot?: boolean;
}) {
  if (data.length < 2) return null;
  const min = Math.min(...data), max = Math.max(...data);
  const rng = max - min || 1;
  const px = (i: number) => (i / (data.length - 1)) * (w - 3) + 1.5;
  const py = (v: number) => h - 2.5 - ((v - min) / rng) * (h - 5);
  const pts = data.map((v, i) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(" ");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="block shrink-0" aria-hidden="true">
      <polyline points={pts} fill="none" stroke={stroke} strokeWidth={1.3} />
      {dot && <circle cx={px(data.length - 1)} cy={py(data[data.length - 1])} r={2} fill={stroke} />}
    </svg>
  );
}
