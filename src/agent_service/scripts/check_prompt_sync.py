# -*- coding: utf-8 -*-
"""프롬프트 정본 ↔ 코드 어긋남 검사 (W43 · LLM 미사용).

**왜 필요한가 (2026-08-06 실측).**
`docs/Agent_프롬프트_라이브러리_v1.md` 가 프롬프트의 **정본**이다(C 소유, 헌법 4-3 "코드를
변경하면 관련 문서도 같은 PR 에서"). 그런데 오늘 하루에만 프롬프트를 **8번** 고치고 문서는
**0번** 고쳤다. 그 결과:

    recipe FEWSHOT      문서 62줄 · 코드 17줄 · 유사도 33%
    limit  FEWSHOT      문서 33줄 · 코드 11줄 · 유사도 41%
    maintenance FEWSHOT 문서 28줄 · 코드 10줄 · 유사도 18%

**이 어긋남은 에러를 내지 않는다.** 코드는 잘 돌고, 문서는 잘 읽힌다 — 둘이 다를 뿐이다.
그래서 사람이 눈으로 대조하지 않으면 영영 안 보이고, 실제로 8/5 하루에만 이탈 2건이 났다.
(헌법 7장의 *"문서·계약에서 형태를 폐기하면서 그걸 읽는 코드를 안 찾음"* 과 같은 종류이나
방향이 반대다 — 이쪽은 **코드를 고치고 문서를 안 고친 것**이다.)

**이 도구는 판정하지 않는다.** 어느 쪽이 맞는지는 사람이 정한다 — 코드가 최신일 수도
(오늘 W56~W62 처럼) 문서가 최신일 수도 있다. 도구는 **무엇이 다른지**를 보여주는 데까지다.

사용:
    python -m src.agent_service.scripts.check_prompt_sync            # 요약
    python -m src.agent_service.scripts.check_prompt_sync --diff     # 줄 단위 차이까지
    python -m src.agent_service.scripts.check_prompt_sync --only recipe

종료 코드: 어긋남 없으면 0, 있으면 1 (CI·훅에 그대로 걸 수 있게).
"""

from __future__ import annotations

import argparse
import difflib
import logging
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("prompt-sync")

DOC = REPO_ROOT / "docs" / "Agent_프롬프트_라이브러리_v1.md"

#: (표시명, 문서 절 제목 패턴, 코드 모듈, 코드 상수)
#  문서는 `### 시스템 프롬프트` 아래 ```text 펜스가 조립된 프롬프트 전문이다.
TARGETS = [
    ("recipe",      r"^## 1\. Tool ①", "src.agent_service.app.tools.recipe",      "SYSTEM_PROMPT"),
    ("limit",       r"^## 2\. Tool ②", "src.agent_service.app.tools.limit",       "SYSTEM_PROMPT"),
    ("maintenance", r"^## 3\. Tool ③", "src.agent_service.app.tools.maintenance", "SYSTEM_PROMPT"),
    ("supervisor",  r"^## 4\. Supervisor", "src.agent_service.app.supervisor",    "SYSTEM_PROMPT"),
    # #140 (2026-08-08) — RTD 해제 근거 Brief. 등재하지 않으면 이 프롬프트만 검사 밖에 남아
    # 같은 표류를 반복한다(W43 이 잡은 것이 바로 그 상태였다).
    ("release",     r"^## 7\. RTD 해제 근거", "src.agent_service.app.release_brief", "SYSTEM_PROMPT"),
    # S9 Copilot (2026-08-11) — release 와 같은 이유로 등재한다. 프롬프트가 하나 늘 때마다
    # 여기 한 줄을 안 더하면 그 프롬프트만 검사 밖에 남고, 표류는 에러를 내지 않는다.
    ("copilot",     r"^## 8\. S9 Agent Copilot", "src.agent_service.app.copilot", "SYSTEM_PROMPT"),
]


def _norm(text: str) -> list[str]:
    """비교용 정규화 — **서식 차이가 아니라 내용 차이만** 보게 한다.

    문서는 마크다운이라 `**강조**`·백틱이 붙고, 코드는 평문이다. 그 차이를 어긋남으로
    세면 전부가 빨개져 아무도 안 본다.
    """
    out = []
    for ln in text.splitlines():
        ln = re.sub(r"\*\*(.+?)\*\*", r"\1", ln)      # 굵게
        ln = re.sub(r"\*(.+?)\*", r"\1", ln)          # 기울임
        ln = ln.replace("`", "").strip()
        ln = re.sub(r"\s+", " ", ln)
        if ln:
            out.append(ln)
    return out


def _doc_section(start_pat: str) -> str:
    """그 절의 `### 시스템 프롬프트` 아래 첫 ```text 펜스 내용."""
    text = DOC.read_text(encoding="utf-8")
    m = re.search(start_pat, text, re.M)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    if nxt:
        rest = rest[: nxt.start()]
    fence = re.search(r"```(?:text)?\n(.*?)```", rest, re.S)
    return fence.group(1) if fence else ""


def _code_prompt(module: str, attr: str) -> str:
    import importlib

    return getattr(importlib.import_module(module), attr)


def check(only: str | None = None, show_diff: bool = False) -> int:
    if not DOC.exists():
        print(f"🔴 정본 문서를 찾을 수 없다: {DOC}")
        return 2

    print("프롬프트 정본 ↔ 코드 어긋남 검사")
    print(f"  정본: {DOC.relative_to(REPO_ROOT)}")
    print("=" * 78)
    print(f"{'대상':<14}{'문서줄':>7}{'코드줄':>7}{'문서만':>7}{'코드만':>7}{'유사도':>8}  판단")
    print("-" * 78)

    drifted = []
    for label, pat, mod, attr in TARGETS:
        if only and only != label:
            continue
        d, c = _norm(_doc_section(pat)), _norm(_code_prompt(mod, attr))
        if not d:
            print(f"{label:<14}{'—':>7}{len(c):>7}{'':>7}{'':>7}{'':>8}  🔴 문서 절을 못 찾음")
            drifted.append((label, [], c))
            continue
        only_doc = [x for x in d if x not in set(c)]
        only_code = [x for x in c if x not in set(d)]
        ratio = difflib.SequenceMatcher(None, "\n".join(d), "\n".join(c)).ratio() * 100
        mark = "✅ 정합" if not (only_doc or only_code) else ("🟡 일부" if ratio >= 70 else "🔴 크게 다름")
        print(f"{label:<14}{len(d):>7}{len(c):>7}{len(only_doc):>7}{len(only_code):>7}{ratio:>7.0f}%  {mark}")
        if only_doc or only_code:
            drifted.append((label, only_doc, only_code))

    print("=" * 78)
    if not drifted:
        print("  ✅ 전부 정합 — 문서와 코드가 같은 말을 한다")
        return 0

    print(f"  🔴 어긋남 {len(drifted)}종")
    print("     · '코드만' = 코드를 고치고 **문서를 안 고친 것** (헌법 4-3 위반 소지)")
    print("     · '문서만' = 문서에 있는데 **코드가 안 따라간 것** (지침이 안 도는 상태)")
    print("     ⚠️ 어느 쪽이 맞는지는 **사람이 정한다** — 이 도구는 다름만 보여준다")
    if show_diff:
        for label, only_doc, only_code in drifted:
            print(f"\n── {label} " + "─" * 60)
            for x in only_code[:12]:
                print(f"  [코드만] {x[:100]}")
            for x in only_doc[:12]:
                print(f"  [문서만] {x[:100]}")
    else:
        print("\n  줄 단위로 보려면: --diff")
    return 1


def main() -> None:
    ap = argparse.ArgumentParser(description="프롬프트 정본 ↔ 코드 어긋남 검사 (W43)")
    ap.add_argument("--diff", action="store_true", help="줄 단위 차이까지 출력")
    ap.add_argument("--only", choices=[t[0] for t in TARGETS], help="한 대상만")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    sys.exit(check(args.only, args.diff))


if __name__ == "__main__":
    main()
