# RAG KB 구축 작업 요약 (MinerU 전환 · 2026-07-07~08)

> 브랜치: `feat/mineru-doc-parsing` · 목표: 장비 매뉴얼·DRAM 슬라이드·데이터 사전을 RAG 지식베이스로 구축.
> 핵심 전환: 문서 파싱을 **Vision LLM(qwen2.5vl) → MinerU 2.5** 로 교체하고, 데이터 사전(C코드)을 스키마대로 적재.

---

## 1. 배경·목표

능동형 제조 AI 플랫폼의 agent_service RAG 지식베이스를 구축한다. 대상은 (a) RIE 장비 매뉴얼(→ error_manual/process_knowledge), (b) DRAM 교육 슬라이드(→ process_knowledge), (c) train_data.csv 데이터 사전(→ sensor_mapping + process_knowledge). 지난 사이클은 로컬 Vision LLM(qwen2.5vl)으로 파싱했으나, 표·구조 보존이 약해 **MinerU 2.5로 전환**을 검토·확정했다. 전 과정은 confidential 자료 보호를 위해 **로컬 오프라인**을 원칙으로 한다.

## 2. 완료 작업 (단계별 상세)

### 2.1 MinerU 2.5 환경 세팅
- 이 PC엔 시스템 Python 3.14뿐(torch/mineru 미지원) → **Python 3.12 전용 venv를 ASCII 경로 `C:\aitch-mineru-venv`에 생성**(uv 사용). 프로젝트가 한글 경로라 그 아래 venv를 두면 MinerU의 FastText(C++)가 언어감지 모델을 못 열어 실패 → **ASCII 경로가 필수**.
- torch는 Windows 기본이 CPU 빌드 → `cu128` 인덱스로 교체(RTX 3070 8GB 활성화). mineru 3.4.2(모델 MinerU2.5).

### 2.2 파서 정확도 3자 실험 (OCR vs QWEN vs MinerU)
- 동일 과제(페이지 충실 전사)로 14페이지 비교. 로컬 VLM 자동심판이 **자기편향**(표 없는 페이지에 "표 깨짐"이라며 MinerU 감점, 형제 QWEN엔 만점)으로 신뢰 불가 → **Claude 직접 채점**으로 전환.

  | 파서 | fidelity | completeness | structure | **종합** |
  |---|:--:|:--:|:--:|:--:|
  | **MinerU 2.5** | 4.93 | 5.0 | 4.64 | **4.86** |
  | QWEN(qwen2.5vl 전사) | 4.5 | 4.36 | 4.14 | 4.33 |
  | OCR(Tesseract) | 3.71 | 4.07 | 2.0 | 3.26 |
- **결론: MinerU 채택.** 승부처는 표 — OCR은 표를 뭉개고 화학식 첨자를 오인식(SF₆→SFs 등)해 RAG에 위험.

### 2.3 KB 문서 파싱 (고유 12종)
- 대상 = RIE 매뉴얼 8(중복 1 포함 → 고유 7) + DRAM 슬라이드 5. 전부 로컬에 존재해 다운로드 불필요.

  | 그룹 | 문서 | 백엔드 | 비고 |
  |---|---|---|---|
  | 매뉴얼(영어) | oxford_sop·Operation-Spec·Oxford_Deep·DRIE_SOP·plasmatherm_790·oxford_plasmalab80 | **vlm-engine** | CJK 오염 0 |
  | 매뉴얼(대형) | Oxford_100_Manual (362p) | **pipeline** | vlm은 ~12시간이라 pipeline |
  | 슬라이드(한국어) | dram_device_physics·jikmu_process·soja_ihae·dongjak_wonri·process_integration | **pipeline** | 도면 많아 vlm 비현실적 |
- 산출물: 문서별 `.md` + `content_list.json`(표=HTML, 이미지=파일 추출).

### 2.4 DRAM 다이어그램 캡셔닝
- pipeline은 이미지를 파일로만 추출·설명 없음 → qwen2.5vl로 >30KB 다이어그램 **539장 캡션**. v1은 약어 환각(PDR을 "Programming Degradation Rate" 등)·CJK 누출 → **v2 개선**(약어 원문유지 강제 + 같은 슬라이드 텍스트 문맥 주입 + CJK 스크럽 → 한자 0). 유효 캡션 523. **단, 캡션 품질은 보이는 라벨은 정확하나 약어 의미를 지어내는 한계가 남음**(§5 참조).

### 2.5 DRAM 개념 노트 5종 + 용어 사전
- 각 덱의 개념을 정리한 `*_concept_note.md` 5종 + 약어 ~150개 `_glossary_dram.md`.
- ⚠️ **중요 한계(정직히)**: 개념 노트는 **이미지에서 추출한 게 아니라, MinerU OCR 텍스트(제목·용어)를 보고 Claude가 표준 반도체 지식으로 채운 것**. 표준 개념엔 정확하나 **슬라이드 그림의 고유 내용(실제 데이터·자사 도식)은 미반영**. → §6 미결 결정사항.

### 2.6 데이터 사전(C코드) → 센서 매핑 + 카드
- train_data.csv C1~C65 유추 사전을 스키마대로 2갈래 적재(원본 표 청킹 금지 — 센서 오검색 방지):
  - `_shared/sensor_mapping.json`: C코드→표준명·단위·카테고리·확정도(65/65, confirmed 45). **룩업 전용(임베딩 X)**.
  - `process_knowledge_sensors.jsonl`: 물리·진단 의미 센서 30종 카드(C32 RF Reflected, C33 PM 카운터, C11 Vdc 등). **임베딩 O**, qdrant_ingest에 등록.

## 3. 핵심 결정·발견

- **8GB GPU vlm 버그**: batch_size=4 자동설정 시 텐서 크래시 → `MINERU_VIRTUAL_VRAM_SIZE=6`(bs=1) + **파일 단위** 파싱으로 회피. **디렉토리 배치(`-p dir`) 금지**(교차문서 배치가 크래시).
- **슬라이드·대형 문서는 pipeline**: vlm은 문서당 10시간+·일부 실패, pipeline은 78p 3분에 완료·품질 충분.
- **자동 심판(로컬 VLM) 자기편향** → 폐기, 사람(Claude) 채점.
- **데이터 사전 원본 표 청킹 금지**(스키마 규칙) → 룩업+큐레이션 카드 구조화, 확정도 등급(✅/🔶) 명시.
- **중복 문서**: `Oxford_Plasmalab_80_Plus` == `oxford_plasmalab80`(md5 동일) → KB 적재 시 하나만.
- **엔지니어 대상 서비스**: 개념 노트는 표준 지식(범용 LLM도 아는 것)이라 **검색 보조**가 목적 — 진짜 고유값은 그림 속 데이터인데 그게 미반영(§6).

## 4. 산출물 위치

```
src/agent_service/
├─ script/mineru_pipeline/
│  ├─ WORK_SUMMARY.md                     # (이 문서)
│  ├─ parser_comparison_report.md         # 파서 3자 비교 리포트
│  ├─ parser_showdown.py / claude_judge.py    # 실험 하네스·채점
│  ├─ caption_diagrams.py                 # 다이어그램 캡셔닝(v2)
│  └─ kb_parse/kb_out/<문서>/{vlm|auto}/
│       ├─ *.md, *_content_list.json      # MinerU 파싱본(텍스트·표)
│       ├─ *_captioned.md                 # 캡션 삽입본(DRAM 슬라이드, 품질 한계 있음)
│       ├─ *_concept_note.md              # DRAM 개념 노트 5종(표준지식 기반)
│       └─ _glossary_dram.md              # 용어·약어 사전 ~150
└─ rag_kb/
   ├─ _shared/sensor_mapping.json         # C코드 룩업(임베딩 X)
   └─ process_knowledge/process_knowledge_sensors.jsonl  # 센서 카드 30(임베딩 O)
```
> ⚠️ 원본 PDF·추출 이미지(.jpg)·MinerU 중간 JSON은 `.gitignore`로 제외(재생성 가능). 텍스트만 커밋.

## 5. 남은 품질 이슈 (인지된 한계)

- **캡션 환각**: qwen 캡션이 그림의 보이는 라벨은 정확히 읽으나(GaAs 결정, Thick/Slim Gox 등), 약어의 뜻을 지어내거나(예: "SN=Signal Node"인데 실제 Storage Node) 문맥 텍스트를 덧붙여 신뢰가 완벽하지 않음. provenance 라벨로 "보조"임을 표시.
- **데이터 사전 불확실성**: C31·C63 등 일부 센서는 성격만 확정·물리명 미상 → 카드에 "미상"으로 명시(과신 방지).

## 6. ⚠️ 열린 결정사항 (내일 결정)

**개념 노트 = 이미지 고유내용 미반영 (표준지식 기반).** 슬라이드 그림/다이어그램이 보여주는 그들만의 데이터·도식은 지금 KB에 제대로 안 들어감(qwen 캡션에만, 신뢰 낮음). 처리 방식을 결정해야 함:
1. **엔지니어(사람)가 핵심 그림만 캡션 검수** — 가장 정확, 소수 그림만.
2. **Claude가 핵심 그림만 직접 판독** — 정확하나 이미지가 클라우드로 감(기밀 판단 필요, 로컬전용 원칙과 충돌).
3. **현행 유지 + 개념 노트를 "표준개념 보조"로 명확 라벨** — 그림 고유내용은 포기.
- 함께 결정: 문서 표현(concept_note provenance, 본 요약)을 "이미지 미반영"으로 더 정직하게 고칠지.

## 7. 다음 단계

1. **청킹·구조화 → Qdrant 적재**(`qdrant_ingest`). "참조" 청크는 자기완결적으로 정리.
2. **검색 테스트**: 엔지니어 스타일 질문 5~10개(golden_set·eval_golden_set.py)로 검색·근거 품질을 **관측**해 "LLM이 이해하는지"를 추측이 아닌 실측으로 확인.
3. (선택) 매뉴얼 파싱본 → error_manual 구조화, §6 결정에 따른 이미지 처리.

## 8. 실행 참고

```powershell
# MinerU 파싱(파일 단위 · bs=1)
$env:MINERU_VIRTUAL_VRAM_SIZE="6"
C:\aitch-mineru-venv\Scripts\mineru.exe -p <pdf> -o <out> -b vlm-engine        # 매뉴얼(영어)
C:\aitch-mineru-venv\Scripts\mineru.exe -p <pdf> -o <out> -b pipeline -l korean # 슬라이드(한국어)
# 캡셔닝 · 적재
C:\aitch-mineru-venv\Scripts\python.exe caption_diagrams.py
python -m src.agent_service.qdrant_ingest    # (docker compose up -d 로 qdrant 필요)
```
