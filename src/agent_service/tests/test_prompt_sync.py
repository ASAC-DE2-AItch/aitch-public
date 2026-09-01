# -*- coding: utf-8 -*-
"""프롬프트 정본 검사기 테스트 (W43 · 2026-08-06).

⚠️ **"어긋남이 0이다"를 단언하지 않는다.** 지금은 실제로 어긋나 있고(오늘 프롬프트를 8번
   고치고 문서는 0번 고쳤다), 그걸 빨간불로 만들면 아무도 안 보는 테스트가 된다.
   여기서 잠그는 것은 **검사기 자체가 제 일을 하는가** — 정규화가 서식만 지우는가,
   절 추출이 맞는 블록을 집는가, 다름을 실제로 잡아내는가.

   "어긋남 0"은 문서를 동기화한 **뒤에** 게이트로 승격한다(그때 이 파일에 한 줄 추가).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.agent_service.scripts import check_prompt_sync as S  # noqa: E402


# --- 정규화: 서식만 지우고 내용은 남기는가 ------------------------------------------
def test_normalize_strips_markdown_only() -> None:
    """문서는 마크다운·코드는 평문이라, 그 차이를 어긋남으로 세면 전부가 빨개진다."""
    doc = "**굵게** 그리고 `백틱` 과 *기울임*"
    code = "굵게 그리고 백틱 과 기울임"
    assert S._norm(doc) == S._norm(code)


def test_normalize_collapses_whitespace_and_drops_blanks() -> None:
    """들여쓰기·빈 줄 차이는 내용 차이가 아니다."""
    assert S._norm("  가  나  \n\n\n  다  ") == ["가 나", "다"]


def test_normalize_keeps_real_difference() -> None:
    """🔴 서식을 지우다 **내용까지** 지우면 검사기가 침묵한다 — 그게 제일 나쁘다."""
    assert S._norm("→ ② process_shift") != S._norm("→ ③ baseline_aging")
    assert S._norm("갭이 낮다") != S._norm("갭이 뚜렷하다")


# --- 절 추출: 맞는 블록을 집는가 ----------------------------------------------------
def test_doc_section_finds_the_text_fence() -> None:
    """`### 시스템 프롬프트` 아래 첫 ```text 펜스가 대상이다."""
    for _, pat, _, _ in S.TARGETS:
        body = S._doc_section(pat)
        assert body, f"{pat}: 절을 못 찾았다 — 문서 제목이 바뀌었을 수 있다"
        assert "[역할]" in body, f"{pat}: 시스템 프롬프트 블록이 아니다"


def test_doc_section_stops_at_next_heading() -> None:
    """다음 `## ` 절을 넘어가면 남의 프롬프트를 섞어 비교하게 된다."""
    recipe = S._doc_section(r"^## 1\. Tool ①")
    assert "Tool ②" not in recipe and "Limit Correction" not in recipe


def test_every_target_maps_to_real_code() -> None:
    """대상 표의 모듈·상수가 실재해야 한다 — 오타면 조용히 빈 문자열이 된다."""
    for label, _, mod, attr in S.TARGETS:
        text = S._code_prompt(mod, attr)
        assert isinstance(text, str) and len(text) > 500, f"{label}: 프롬프트가 비었다"


# --- 검사 결과: 다름을 실제로 잡는가 ------------------------------------------------
def test_prompt_and_doc_are_in_sync() -> None:
    """🔒 **게이트** — 코드 프롬프트와 정본 문서가 어긋나면 실패한다 (2026-08-06 승격).

    승격 전에는 *"어긋나 있으므로 1 이어야 한다"* 를 단언했다. 그날 동기화를 마쳐
    4/4 · 100% 정합이 됐으므로 방향을 뒤집는다.

    ⚠️ **이 테스트가 빨개지면 문서를 손으로 고치지 마라.** 코드를 고친 뒤
    `python -m src.agent_service.scripts.check_prompt_sync --diff` 로 무엇이 다른지 보고,
    코드가 맞으면 동기화 스크립트로 펜스를 다시 박는다. 손으로 옮기면 또 어긋난다 —
    그게 유사도 37% 까지 벌어진 경위다(헌법 4-3).
    """
    assert S.check() == 0, (
        "프롬프트 정본과 코드가 어긋났다 — "
        "`python -m src.agent_service.scripts.check_prompt_sync --diff` 로 확인할 것"
    )


def test_removed_rule_is_gone_from_both(capsys) -> None:
    """🔴 **제거한 지침이 문서에 되살아나지 않는가.**

    *"② 아님 → ④ escalate"* 는 W57 에서 뺀 줄이고, 그 지침 때문에 round19 에서 13건이
    오판됐다. 동기화 전까지 **문서에는 살아 있었다** — 문서 기준으로 되돌렸으면 재발했다.
    코드·문서 **양쪽에서** 사라졌는지 확인한다.
    """
    from src.agent_service.app.supervisor import SYSTEM_PROMPT

    doc = S.DOC.read_text(encoding="utf-8")
    for ghost in ("② 아님 → ④ escalate", "손잡이 히트가 전혀 없으면"):
        assert ghost not in SYSTEM_PROMPT, f"코드에 {ghost!r} 가 남아 있다"
        assert ghost not in doc, f"문서에 {ghost!r} 가 되살아났다"
