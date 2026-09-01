"""3조수(tool) 패키지 — Recipe·실력치·정비 병렬 팬아웃 대상.

TOOLS 는 파이프라인이 asyncio 로 병렬 실행하는 tool 목록의 단일 소스다 (헌법 1-2 병렬 원칙).
새 tool 추가/제거는 여기 한 곳만 고치면 팬아웃·테스트에 반영된다.
"""

from __future__ import annotations

from . import limit, maintenance, recipe
from .base import AgentTool

# 팬아웃 순서 = 아키텍처 다이어그램 §2 (레시피·실력치·정비). 병렬이라 결과 순서만 안정 보장.
TOOLS: tuple[AgentTool, ...] = (recipe, limit, maintenance)

__all__ = ["TOOLS", "AgentTool", "recipe", "limit", "maintenance"]
