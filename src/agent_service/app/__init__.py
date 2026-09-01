"""Agent Service 런타임 패키지 (팀원 C).

fdc.alert 를 수신해 논리 Agent(tool) 3종(Recipe·실력치·정비)을 병렬 실행하고
Supervisor 판단 로직으로 통합 Brief 를 생성한다 (헌법 1-2·1-4).

이 `app/` 패키지는 서비스 런타임 코드만 담는다. KB 구축 스크립트(rag_kb/·chunking.py 등)와
분리된 경계를 유지한다.
"""
