"""Evidence Card + 리포트 스키마 검증 테스트 (C4-2).

§5 근거 카드 규칙을 잠그는 회귀 테스트:
  · evidence 2~4개 (미만/초과 거부)
  · 서로 다른 source_type 2종 이상 (한 계열만 거부)
  · counter_evidence 필수 · extra 필드 금지
  · 리포트(OptionReportBase 파생)에 카드 부착 · 빈 리스트(스텁) 허용
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pydantic import ValidationError  # noqa: E402

from agent_service.app.schemas.report import (  # noqa: E402
    EvidenceCard,
    LimitOption,
)


def _card(**overrides) -> dict:
    """유효한 카드 dict (§5 예시 기반). overrides로 일부만 바꿔 실패 케이스 생성."""
    base = {
        "card_id": "EC-LIM-20260713-SIMCH3-0412-1",
        "claim": "C11 상승은 기준선 노후이지 장비 이상이 아니다",
        "evidence": [
            {"source_type": "spc", "ref": "ALERT-20260713-SIMCH3-0412", "snippet": "N3 추세, 급변 없음"},
            {"source_type": "model", "ref": "prediction_context", "snippet": "predicted_c65 715 정상"},
            {"source_type": "case", "ref": "CASE-0077 (sim 0.88)", "snippet": "재설정 후 재발 없음"},
        ],
        "counter_evidence": "이동 속도 과거 대비 1.4배 — 열화 전조 가능성 [SPC]",
        "uncertainty": "PM 직후 데이터가 재산정 창에 일부 포함",
    }
    base.update(overrides)
    return base


# --- 정상 카드 ----------------------------------------------------------------
def test_valid_card_parses() -> None:
    """§5 예시대로면 통과 — evidence 3개·출처 3종·counter 있음."""
    card = EvidenceCard.model_validate(_card())
    assert len(card.evidence) == 3
    assert card.display_note is None  # optional 미지정


# --- evidence 개수 (2~4) -------------------------------------------------------
def test_single_evidence_rejected() -> None:
    """evidence 1개는 min_length=2 위반."""
    with pytest.raises(ValidationError):
        EvidenceCard.model_validate(
            _card(evidence=[{"source_type": "spc", "ref": "X", "snippet": "one"}])
        )


def test_five_evidence_rejected() -> None:
    """evidence 5개는 max_length=4 위반."""
    five = [{"source_type": "spc", "ref": f"X{i}", "snippet": str(i)} for i in range(5)]
    with pytest.raises(ValidationError):
        EvidenceCard.model_validate(_card(evidence=five))


# --- 출처 2종 이상 -------------------------------------------------------------
def test_same_source_type_rejected() -> None:
    """evidence가 전부 spc면 '출처 2종' 규칙 위반 (한 계열 결론 금지 §5)."""
    same = [
        {"source_type": "spc", "ref": "A", "snippet": "1"},
        {"source_type": "spc", "ref": "B", "snippet": "2"},
    ]
    with pytest.raises(ValidationError, match="source_type"):
        EvidenceCard.model_validate(_card(evidence=same))


def test_two_distinct_source_types_ok() -> None:
    """서로 다른 출처 2종이면 통과(경계값)."""
    two = [
        {"source_type": "spc", "ref": "A", "snippet": "1"},
        {"source_type": "case", "ref": "CASE-1", "snippet": "2"},
    ]
    card = EvidenceCard.model_validate(_card(evidence=two))
    assert len(card.evidence) == 2


# --- 필수/금지 필드 ------------------------------------------------------------
def test_counter_evidence_required() -> None:
    """counter_evidence 누락은 거부 — 반증 필수 (§5)."""
    d = _card()
    del d["counter_evidence"]
    with pytest.raises(ValidationError):
        EvidenceCard.model_validate(d)


def test_unknown_source_type_rejected() -> None:
    """source_type enum 밖(rumor 등)은 거부."""
    with pytest.raises(ValidationError):
        EvidenceCard.model_validate(
            _card(evidence=[
                {"source_type": "rumor", "ref": "A", "snippet": "1"},
                {"source_type": "case", "ref": "CASE-1", "snippet": "2"},
            ])
        )


def test_extra_field_forbidden() -> None:
    """계약 밖 필드는 outbound 엄격(extra=forbid)으로 조기 차단."""
    with pytest.raises(ValidationError):
        EvidenceCard.model_validate(_card(made_up="oops"))


# --- 리포트 부착 --------------------------------------------------------------
def test_report_accepts_evidence_cards() -> None:
    """LimitOption 리포트에 카드를 부착해 파싱 — 타입이 EvidenceCard로 승격됐는지."""
    rep = LimitOption(
        report_id="LIM-20260713-SIMCH3-0412",
        confidence=0.88,
        rationale="추세 룰 위주 [SPC]",
        uncertainty="재산정 창 치우침",
        evidence_cards=[_card()],
    )
    assert isinstance(rep.evidence_cards[0], EvidenceCard)


def test_report_empty_cards_ok() -> None:
    """스텁/에스컬 단계의 빈 카드 리스트는 정상 (default_factory)."""
    rep = LimitOption(
        report_id="LIM-20260713-SIMCH3-0412",
        confidence=0.0,
        rationale="stub",
        uncertainty="stub",
    )
    assert rep.evidence_cards == []
