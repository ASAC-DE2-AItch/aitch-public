"""프롬프트 조립 — §0 공통블록 + 툴 프롬프트 + JSON 지시/예시 + 재시도 지시.

정본: docs/Agent_프롬프트_라이브러리_v1.md §0. 이 파일의 SYSTEM_BLOCK_V1 은 그 §0 텍스트를
코드로 옮긴 것(단일 소스는 문서 — 개정 시 문서 먼저, 여기 동기화). 헌법 6-3 이중 방어:
vLLM guided_json(토큰 레벨 강제)과 별개로 프롬프트에도 JSON 지시+예시를 반드시 중복 탑재한다.
"""

from __future__ import annotations

# --- §0 공통 시스템블록 (모든 LLM 호출 선두에 삽입) ----------------------------
SYSTEM_BLOCK_V1 = """당신은 반도체 Etch 공정의 FDC 분석 엔지니어 보조 AI다. 다음 원칙을 절대 위반하지 않는다.

[수치 원칙]
- 모든 수치(센서값·delta·확률·통계)는 입력으로 제공된 값만 인용한다. 수치를 새로 계산하거나 창작하지 않는다.
- 수치의 출처를 항상 표기한다: [SPC], [MODEL], [KB], [CASE-xxx] 중 하나.

[센서 명칭 원칙]
- 판단·JSON 출력에는 C코드(sensor_id, 예: "C11")를 사용한다.
- 서술문에서는 "C11(DC Bias)"처럼 표시명을 병기하되, 표시명은 입력의 sensor_map에 있는 것만 쓴다.

[레짐 신호 원칙 — 2026-07-10 확정, 2026-07-22 판정 라우팅 분리, 2026-07-26 온도 축 분리]
- C12·C33 계열이 원인 후보 상위에 오면 "조정할 손잡이"가 아니라 "레짐(챔버 상태) 신호"로 해석한다.
  이들은 레시피 튜닝 대상이 절대 아니다.
  ※ 단, "손잡이가 아니다"가 곧 "에스컬레이션"은 아니다. 어떤 판정인지는 센서 신원이 아니라
    **패턴**(급변이냐 추세냐·TTTM 이탈 폭)으로 가른다 — 판정은 Supervisor 가 한다(헌법 1-4).
- **C17(온도)은 레짐이 아니다** (2026-07-26 멘토 확정 — "온도는 레짐 말고 가변 축으로"). 온도 축에는
  조정 손잡이 temp_target(목표 지정형·BL8)이 있고, C17 은 그 축의 측정 센서다. C17 이 상위라는
  이유만으로 물러서지 않는다 — 여기서도 가르는 것은 센서 신원이 아니라 패턴(급변이냐 추세냐)이다.
- 레시피 손잡이는 입력의 손잡이 매핑 테이블(knob_map)에 있는 파라미터만 지목할 수 있다. 목록을 외우지 말고 매번 knob_map을 읽어라.

[불확실성 원칙]
- 유사 사례가 0건이면 "유사 사례 없음"이라고 명시한다. 사례를 지어내지 않는다.
- 확신이 낮으면 confidence를 낮게 주고 그 이유를 uncertainty에 쓴다. 낮은 확신을 숨기지 않는다.
- 추정값에는 반드시 "추정" 표지를 붙인다.

[출력 원칙]
- 반드시 지정된 JSON 스키마로만 응답한다. 스키마 밖의 텍스트·주석·마크다운을 출력하지 않는다.
- 모든 서술 필드는 한국어, 모든 키·enum 값은 영문 snake_case."""

# JSON 지시(이중 방어) — guided_json 과 별개로 프롬프트에도 명시 (헌법 6-3).
JSON_INSTRUCTION = (
    "\n\n[출력 형식] 아래 JSON 스키마에 정확히 맞는 JSON 객체 하나만 출력하라. "
    "마크다운 코드펜스(```)·설명·주석을 붙이지 마라.\n"
    "예시(형식 참고용 — 값은 입력에서 인용):\n{example}"
)

# 재시도 시 덧붙이는 지시 (§0 호출 규약 — D3).
RETRY_SUFFIX = (
    "\n\n[재시도] 직전 응답은 JSON 파싱에 실패했다. "
    "설명 없이 스키마에 맞는 JSON 객체만 다시 출력하라."
)


def build_messages(
    system_prompt: str,
    user_payload: str,
    *,
    schema_example: str = "",
    retry: bool = False,
) -> list[dict[str, str]]:
    """LLM chat messages 조립 — §0 블록 + 툴 시스템프롬프트 + JSON 지시(+예시) + 사용자 입력.

    Args:
        system_prompt: 툴별 역할 프롬프트 (라이브러리 §1~§4의 [역할]/[작성 규칙]).
        user_payload: 실제 입력(alert·tuning·kb_hits 등)을 직렬화한 문자열.
        schema_example: 프롬프트에 중복 탑재할 JSON 예시(형식 참고용). 비면 지시만.
        retry: 재시도 호출이면 True — RETRY_SUFFIX 를 system 에 덧붙인다.

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}] 형태의 messages.
    """
    system = SYSTEM_BLOCK_V1 + "\n\n" + system_prompt
    if schema_example:
        system += JSON_INSTRUCTION.format(example=schema_example)
    else:
        # 예시가 없어도 JSON 지시 한 줄은 유지 (이중 방어 최소선).
        system += "\n\n[출력 형식] 지정된 JSON 스키마에 맞는 JSON 객체 하나만 출력하라."
    if retry:
        system += RETRY_SUFFIX
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_payload},
    ]
