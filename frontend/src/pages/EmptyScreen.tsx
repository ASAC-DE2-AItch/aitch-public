// 미구축 화면 — 벤토 위젯 스타일
export default function EmptyScreen({ screenId, title, plannedTask }: {
  screenId: string; title: string; plannedTask: string;
}) {
  return (
    <div className="widget widget-pad flex flex-col items-start gap-2" style={{ padding: 28 }}>
      <span className="chip chip-neutral">{screenId}</span>
      <h2 className="big-stat" style={{ fontSize: 24 }}>{title}</h2>
      <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>구축 예정 — {plannedTask}</p>
    </div>
  );
}
