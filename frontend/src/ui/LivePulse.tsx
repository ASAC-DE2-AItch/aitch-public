import { useEffect, useRef } from "react";
import type { PulseItem } from "../mock/fdc";

type Items = (PulseItem & { fresh: boolean })[];

/** 가로 티커 — 새 wafer 칸이 왼쪽에서 들어오며 기존 칸들을 오른쪽으로 한 칸씩 연속으로 민다.
 *  7칸 렌더 / 뷰포트 6칸: 애니메이션 시작 시점(-1칸)이 직전 상태와 동일해 끊김 없이 이어진다. */
export default function LivePulse({ items }: { items: Items }) {
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
  }, []);

  const headId = items[0]?.wafer ?? "empty";

  return (
    <div className="card pulse">
      {/* 라이브 링크 점 — 전역 점멸 문법(syncdot): 사이드바·Copilot과 같은 위상 */}
      <div className="plabel"><span className="syncdot" style={{ width: 7, height: 7, borderWidth: 2 }} /><span className="t">Live Wafer · C65</span></div>
      <div className="pviewport">
        <div className={"prow" + (mounted.current ? " slide" : "")} key={headId}>
          {items.slice(0, 7).map((p) => (
            <div className="pcell" key={p.wafer} title={`${p.wafer} · C65 ${p.c65.toLocaleString()}`}>
              <span className="pch">{p.ch}</span>
              <span className="w">{p.lot} #{p.wafer.split("-").pop()}</span>
              <span className="c num">{p.c65.toLocaleString()}</span>
              <span className={"schip " + (p.warn ? "warn" : "ok")}>{p.warn ? "WARN" : "OK"}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
