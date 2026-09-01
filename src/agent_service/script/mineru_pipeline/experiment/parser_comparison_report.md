# 문서 파서 정확도 비교 실험 리포트 — OCR vs QWEN vs MinerU 2.5

> 장비 매뉴얼 RAG 지식베이스 구축을 위한 **문서 파싱 백엔드 선정** 실험.
> 지난 사이클은 Vision LLM(QWEN)으로 파싱했고, 이번에 MinerU 2.5로 전환을 검토하며 3종을 정면 비교함.

- **작성일:** 2026-07-07
- **결론(요약):** **MinerU 2.5 (4.86) ≳ QWEN (4.33) ≫ OCR (3.26)** / 5점 만점 → **파싱 백엔드는 MinerU 2.5 채택 권장**

---

## 1. 실험 목적

세 가지 문서 파싱 방식이 **장비 매뉴얼 페이지를 얼마나 정확히 텍스트로 뽑아내는가**를 정량 비교한다.
표·화학식·절차·그림이 섞인 반도체 Etch 장비 매뉴얼에서, RAG 검색 품질을 좌우하는 것은 파싱 정확도이므로 이를 단독 변인으로 측정한다. (검색 품질 A/B는 후속 단계)

## 2. 비교 대상 (파서 3종)

| 코드 | 방식 | 버전/모델 | 실행 설정 |
|---|---|---|---|
| **OCR** | Tesseract OCR | tesseract v5.4.0.20240606 (leptonica 1.84.1) | lang=`eng`, 페이지 렌더 200 DPI, `pytesseract` |
| **QWEN** | Vision LLM 페이지 전사 | `qwen2.5vl:7b` (Ollama, 로컬) | temperature=0, num_ctx=8192, **충실 전사 프롬프트**, 200 DPI |
| **MinerU** | MinerU 2.5 (문서 파싱 VLM) | mineru **3.4.2** (모델 MinerU2.5), 백엔드 `vlm-engine` | GPU 추론, `MINERU_MODEL_SOURCE=huggingface` |

> **공정성 핵심 — 동일 과제로 통일:** 원래 세 도구는 산출물 형태가 다르다(OCR=raw text, 기존 QWEN 크롤러=symptom/cause/action 의미추출 JSON, MinerU=markdown). 비교를 공정하게 하기 위해 **모두 "페이지를 있는 그대로 Markdown으로 충실히 전사"라는 동일 과제**로 맞췄다. QWEN도 의미추출 프롬프트가 아니라 전사 프롬프트로 실행.

## 3. 실험 환경

| 항목 | 값 |
|---|---|
| GPU | NVIDIA GeForce RTX 3070 8GB (CUDA driver 591.86) |
| Python | 3.12.13 (전용 venv, **ASCII 경로** `C:\aitch-mineru-venv`) |
| PyTorch | 2.11.0+cu128 (CUDA) |
| 렌더링 | poppler(pdftoppm) 25.07.0, 200 DPI |
| 추론(QWEN·심판) | Ollama `qwen2.5vl:7b` (오프라인) |

> ⚠️ **한글 경로 주의:** MinerU가 쓰는 FastText(C++)는 경로에 한글이 있으면 언어감지 모델을 못 연다. 프로젝트가 `…\문서\…\sk-하이닉스\…` 한글 경로라, venv를 **ASCII 경로**(`C:\aitch-mineru-venv`)에 두어 회피했다.

## 4. 대상 문서

| 문서 | 페이지 | 성격 |
|---|:--:|---|
| `oxford_sop.pdf` | 6 | 절차(SOP) 위주 + 장비 사진, 표 거의 없음 |
| `Operation-Spec-Oxford-System-100-Rev-C.pdf` | 8 | 스펙/표 위주 (Recipe·DRIE 파라미터·화학식 표) |

두 문서 모두 Oxford Plasmalab 100 ICP-RIE 계열 매뉴얼 = **총 14페이지**. 절차형·표형 두 성격을 모두 커버.

## 5. 채점 방법

- **채점자: Claude.** 각 페이지의 **원본 이미지**를 직접 보고 3종 전사본과 대조해 채점(0~5).
- **채점 기준(각 0~5점):**
  - `fidelity` — 텍스트/기술용어 정확도 (오탈자·오인식 없이 원문 일치)
  - `completeness` — 누락 없이 원본 내용 전부 포함
  - `structure` — 제목·표·목록 등 구조 보존 (특히 표의 행/열)
- **자동 심판(로컬 VLM) 폐기 경위:** 초기엔 `qwen2.5vl` 자동 심판을 썼으나, **표가 아예 없는 페이지**의 MinerU 출력에 "표 구조가 완전히 흐트러짐"이라며 structure=1을 주고 자기 형제 모델 QWEN엔 5/5/5를 부여하는 **자기편향+환각**이 확인되어 폐기하고, 사람(Claude) 직접 채점으로 전환했다.
- **공정성 보정:** 채점 전, MinerU 페이지 슬라이스에서 `table_caption`(표 제목)·`header`·`page_number`를 누락하던 **하네스 버그를 먼저 수정**했다(수정 없이는 MinerU가 부당하게 감점됨).

---

## 6. 결과

### 6.1 종합 (14페이지 평균)

| 파서 | fidelity 정확도 | completeness 누락없음 | structure 구조/표 | **종합** | 페이지 우승 |
|---|:--:|:--:|:--:|:--:|:--:|
| **MinerU 2.5** | **4.93** | **5.0** | **4.64** | **4.86** | 11.3 |
| QWEN (qwen2.5vl 전사) | 4.5 | 4.36 | 4.14 | 4.33 | 2.3 |
| OCR (Tesseract) | 3.71 | 4.07 | 2.0 | 3.26 | 0.3 |

**→ MinerU 2.5 ≳ QWEN ≫ OCR**

### 6.2 문서별 평균

**oxford_sop (절차/사진)**

| 파서 | fidelity | completeness | structure |
|---|:--:|:--:|:--:|
| MinerU 2.5 | 5.0 | 5.0 | 5.0 |
| QWEN | 4.0 | 4.0 | 4.0 |
| OCR | 3.83 | 4.0 | 2.0 |

**Operation-Spec (표/스펙)**

| 파서 | fidelity | completeness | structure |
|---|:--:|:--:|:--:|
| MinerU 2.5 | 4.88 | 5.0 | 4.38 |
| QWEN | 4.88 | 4.62 | 4.25 |
| OCR | 3.62 | 4.12 | 2.0 |

### 6.3 페이지별 점수 (fidelity/completeness/structure)

| 문서 | p | 유형 | OCR | QWEN | MinerU | 우승 | 관찰 |
|---|:--:|:--:|:--:|:--:|:--:|:--:|---|
| oxford_sop | 1 | image | 4/4/2 | 4/4/4 | 5/5/5 | MinerU | OCR 구조소실 · QWEN 이미지경로 소실·메타를 표로 재구성 · MinerU 이미지 실제추출+충실 |
| oxford_sop | 2 | text | 3/4/2 | 4/4/4 | 5/5/5 | MinerU | OCR 가스식 오인식(BCI3/02/C12) · QWEN BCl3→BC13·원문오타 'Suppied'→'Supplied' 무단교정·섹션번호 소실 · MinerU 전부 정확+번호 유지 |
| oxford_sop | 3 | image | 4/4/2 | 4/4/4 | 5/5/5 | MinerU | MinerU 사진 2장 실제 파일추출+캡션 · QWEN 플레이스홀더뿐 |
| oxford_sop | 4 | text | 4/4/2 | 4/4/4 | 5/5/5 | MinerU | 평문 절차 — 패턴 동일 |
| oxford_sop | 5 | text | 4/4/2 | 4/4/4 | 5/5/5 | MinerU | 평문 절차 — 패턴 동일 |
| oxford_sop | 6 | text | 4/4/2 | 4/4/4 | 5/5/5 | MinerU | 평문 절차 — 패턴 동일 |
| Op-Spec | 1 | text | 4/4/2 | 5/4/4 | 5/5/5 | MinerU | QWEN 섹션번호 I. 소실 · MinerU 계층+번호 유지 |
| Op-Spec | 2 | text | 4/4/2 | 5/4/4 | 5/5/5 | MinerU | OPERATION 절차 평문 — 패턴 동일 |
| Op-Spec | 3 | blank | 5/5/3 | 5/5/3 | 5/5/3 | 동점 | 거의 백지(헤더+푸터뿐). MinerU는 헤더/푸터를 별도 타입으로 분리(RAG 노이즈 제거에 유리) |
| Op-Spec | 4 | table | 3/4/2 | 4/5/5 | 5/5/5 | MinerU | OCR 잡음·표 평탄화 · QWEN·MinerU 표 정상(QWEN 섹션번호 V. 소실) |
| Op-Spec | 5 | text | 4/4/2 | 5/5/4 | 5/5/4 | 동점 | Appendix A 목록. MinerU 중첩불릿 '- -' 사소한 흠 |
| Op-Spec | 6 | table | 2/4/1 | 5/5/5 | 5/5/5 | 동점 | 화학식 표. OCR 오인식 다발(Si3Na/SiO,/Oz/CHF;)+평탄화 · QWEN·MinerU 표·첨자 정확 |
| Op-Spec | 7 | text | 4/4/2 | 5/4/4 | 5/5/4 | MinerU | Appendix C 평문 — 패턴 동일 |
| Op-Spec | 8 | table | 3/4/2 | 5/5/5 | 4/5/4 | QWEN | 핵심 표 3개. OCR 평탄화·첨자오류 · QWEN 완벽 · MinerU 값·제목·구조 정확하나 첨자표기 불일치($SF_6$ vs SF6) |

## 7. 핵심 발견

1. **표 정확도 = 승부처.** OCR은 모든 표를 공백 나열로 뭉개 **열 구분이 사라지고**(Recipe 표에서 어느 숫자가 어느 파라미터인지 복원 불가), 화학식 첨자를 오인식(SF₆→SFs, O₂→Oz/02, Cl₂→C12, SiO₂→SiOz, Si₃N₄→Si3Na, CHF₃→CHF;) → RAG 지식으로 쓰기 위험.
2. **QWEN·MinerU 모두 표를 정확히 구조화**(QWEN=Markdown 표, MinerU=HTML 표). 값·행·열 모두 정확.
3. **MinerU 우위 요소(RAG 실전):**
   - ① 섹션 번호/계층(I·V·3·4·5) 보존
   - ② **그림을 실제 파일로 추출 + VLM 캡션** (QWEN은 `![](image)` 플레이스홀더뿐)
   - ③ **표 제목(Table 2/3/4…) 보존**
   - ④ 단일 결정적 도구라 원문을 임의 변경하지 않음
4. **QWEN 위험 요소:** 원문 오타를 **임의 교정**(‘Suppied’→‘Supplied’)하고 섹션 번호를 종종 누락. LLM 전사라 드물게 값 변조 가능성 — KB에선 치명적.
5. **MinerU 사소한 흠:** 표 첨자 표기 불일치($SF_6$ vs SF6), 중첩 불릿 `- -`, 헤더/푸터 reading-order 흔들림 — 모두 후처리 정규화로 보완 가능.

## 8. 권고 및 다음 단계

- **파싱 백엔드는 MinerU 2.5 채택** 권장: 표·그림·구조 보존이 RAG 검색 품질에 직결. OCR 대비 압도적, QWEN 대비 구조·그림·안전성(무단변경 없음)에서 우위.
- MinerU 첨자/불릿 표기는 간단한 정규화 후처리로 보완.
- **다음 단계:** MinerU markdown → (경로1) 계층 청킹 / (경로2) 로컬 LLM 구조화(symptom/cause/action) → `*_mineru.jsonl` → 별도 `_mineru` 컬렉션 적재 후 golden_set으로 **검색 품질(Recall/MRR)까지** A/B. (이번 실험은 '파싱 정확도' 단계)

## 9. 재현 방법

```powershell
# 1) 14페이지 3종 파싱 (자동심판 없이 산출물만)
C:\aitch-mineru-venv\Scripts\python.exe `
  src\agent_service\script\mineru_pipeline\experiment\parser_showdown.py --no-judge

# 2) 채점 + 리포트 생성 (Claude 채점 결과가 코드에 인코딩됨)
C:\aitch-mineru-venv\Scripts\python.exe `
  src\agent_service\script\mineru_pipeline\experiment\claude_judge.py
```

**산출물 위치:**
- 종합 리포트: `exp_out/report_claude.md` , 점수 JSON: `exp_out/claude_scores.json`
- 페이지별 원문·전사본(재검증용): `exp_out/<doc>/p<NN>/{page.png, ocr.txt, qwen.md, mineru.md}`
- MinerU 원본 파싱: `exp_out/<doc>/_mineru/**/{*.md, *_content_list.json, images/}`

## 부록. 왜 자동 심판(qwen2.5vl)을 버렸나

스모크 테스트에서 자동 심판이 **oxford_sop p1(표가 아예 없는 페이지)** 의 MinerU 출력에 "표 구조가 완전히 흐트러짐"이라며 structure=1을 주고, 자기 형제 모델 QWEN엔 5/5/5를 부여했다. 존재하지 않는 결함을 지어내는 **자기편향+환각**이 확인되어 사람(Claude) 직접 채점으로 전환했다. 원본·전사본은 `exp_out/<doc>/p<NN>/`에 모두 보존되어 언제든 재검증 가능하다.
