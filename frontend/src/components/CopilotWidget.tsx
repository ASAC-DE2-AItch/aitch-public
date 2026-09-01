// Agent Copilot 위젯 — AURA 벤토 시민 (챗 카드 + 다크 필 CTA)
import { useState } from "react";
import { Link } from "react-router-dom";
import { Bot, ArrowUp } from "lucide-react";
import { CHAMBER_SUMMARIES, PENDING_INCIDENTS } from "../mock/monitoring";
import { useGlobal } from "../state/GlobalContext";

interface Msg { role: "user" | "agent"; text: string }

const REPLY: Msg = {
  role: "agent",
  text: "SHAP 상위 기여(C4 0.41)·유사 Case 3건 근거로 Recipe 경로가 우세해요. 승인은 인시던트 화면에서.",
};

export default function CopilotWidget() {
  const { chamber } = useGlobal();
  const focus =
    CHAMBER_SUMMARIES.find((c) => c.chamberId === chamber) ??
    CHAMBER_SUMMARIES.reduce((a, c) => (c.driftScore > a.driftScore ? c : a));
  const [msgs, setMsgs] = useState<Msg[]>([
    { role: "user", text: `${focus.chamberId.replace("SIM_", "")} 왜 recipe 추천이야?` },
    REPLY,
  ]);
  const [input, setInput] = useState("");
  const topInc = PENDING_INCIDENTS[0];

  const ask = (q: string) => {
    if (!q.trim()) return;
    setMsgs((m) => [...m, { role: "user", text: q.trim() }, REPLY]);
    setInput("");
  };

  return (
    <div className="widget widget-pad flex h-full flex-col">
      <div className="flex items-center gap-2">
        <span className="flex h-7 w-7 items-center justify-center rounded-full"
          style={{ background: "var(--accent-soft)", color: "var(--accent)" }}>
          <Bot size={14} />
        </span>
        <span className="text-[13.5px] font-bold" style={{ color: "var(--text)" }}>Copilot</span>
        <span className="chip chip-neutral ml-auto" style={{ fontSize: 10 }}>
          ctx {focus.chamberId.replace("SIM_", "")}
        </span>
      </div>

      <div className="mt-3 min-h-0 flex-1 space-y-2 overflow-y-auto">
        {msgs.map((m, i) => (
          <div key={i}
            className="rounded-2xl px-3 py-2 text-[12px] leading-relaxed"
            style={{
              background: m.role === "user" ? "var(--accent-soft)" : "var(--widget-2)",
              color: "var(--text)",
              marginLeft: m.role === "user" ? 18 : 0,
              marginRight: m.role === "agent" ? 18 : 0,
              fontWeight: m.role === "user" ? 600 : 450,
            }}>
            {m.text}
          </div>
        ))}
        {topInc && (
          <Link to={`/incidents/${topInc.incidentId}`} className="btn-dark w-full" style={{ padding: "9px 14px", fontSize: 12 }}>
            최우선 인시던트 열기
          </Link>
        )}
      </div>

      <div className="mt-3 flex items-center gap-2 rounded-full px-3.5 py-1"
        style={{ background: "var(--widget-2)", border: "1px solid var(--border)" }}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ask(input)}
          placeholder="질문하기…"
          className="w-full bg-transparent py-1.5 text-[12px] outline-none"
          style={{ color: "var(--text)" }}
        />
        <button type="button" onClick={() => ask(input)} aria-label="질문 보내기"
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full"
          style={{ background: "var(--accent)", color: "#fff" }}>
          <ArrowUp size={12} />
        </button>
      </div>
    </div>
  );
}
