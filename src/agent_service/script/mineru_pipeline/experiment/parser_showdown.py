#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
파서 정확도 3자 비교 실험 — OCR(Tesseract) vs QWEN(qwen2.5vl 전사) vs MinerU 2.5
================================================================================

목적: "어떤 문서 파서가 페이지를 가장 정확히 텍스트로 뽑아내는가"를 정량 비교.

세 도구는 원래 산출물 형태가 다르지만(OCR=raw text, 기존 QWEN 크롤러=의미추출 JSON,
MinerU=markdown), 공정한 비교를 위해 **동일 과제 = "페이지를 있는 그대로 Markdown으로
충실히 전사"** 로 맞춘다. QWEN도 의미추출 프롬프트가 아니라 '충실 전사' 프롬프트로 돌린다.

정답 전사본이 없으므로, **원본 페이지 이미지를 직접 본 VLM 심판(qwen2.5vl)** 이
세 전사 결과를 블라인드(A/B/C 무작위 라벨)로 채점한다:
  - fidelity(텍스트 정확도) · completeness(누락 없음) · structure(제목/표/목록 보존)  각 0~5

한계(리포트에도 명시): 심판이 후보 중 하나(QWEN)와 같은 모델이라 자기편향 가능 →
블라인드+라벨 무작위화로 완화하고, 원문 나란히보기(side-by-side)로 사람이 교차검증한다.

산출물:
  exp_out/<doc>/p{NN}/{page.png, ocr.txt, qwen.md, mineru.md, judge.json}
  exp_out/scores.json   (집계 점수)
  exp_out/report.md     (점수표 + 페이지별 나란히보기)

실행(반드시 ASCII 경로 venv로):
  C:\\aitch-mineru-venv\\Scripts\\python.exe -m ...experiment.parser_showdown [--max-pages N] [--force]
사전조건: Ollama(qwen2.5vl:7b) 기동, tesseract·poppler 설치, mineru 설치.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytesseract
from pdf2image import convert_from_path
from PIL import Image

# winget 설치 시 PATH가 즉시 갱신되지 않는 환경(Windows)을 위한 tesseract 경로 설정 (ocr.py와 동일 방식)
_TESSERACT_FALLBACK = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
_tess = shutil.which("tesseract")
if _tess:
    pytesseract.pytesseract.tesseract_cmd = _tess
elif Path(_TESSERACT_FALLBACK).exists():
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_FALLBACK

for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ [Showdown] %(message)s")
log = logging.getLogger("showdown")

# --- 경로 설정 -----------------------------------------------------------------
HERE = Path(__file__).resolve().parent
EXP_OUT = HERE / "exp_out"
MANUALS_DIR = Path(
    r"C:\Users\cindy\OneDrive\문서\project\Bootcamp\sk-하이닉스\AITCH-r2r-c\Data\source_pdfs\manuals"
)
DOCS = [
    MANUALS_DIR / "oxford_sop.pdf",                              # 절차 위주(표 없음)
    MANUALS_DIR / "Operation-Spec-Oxford-System-100-Rev-C.pdf",  # 스펙/표 위주
]

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
QWEN_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5vl:7b")
DPI = 200
JUDGE_CHAR_CAP = 3500  # 심판 프롬프트에 넣는 후보 전사문 최대 길이(컨텍스트 초과 방지)
RNG_SEED = 42          # 블라인드 라벨 무작위화 재현용

TOOLS = ("ocr", "qwen", "mineru")


# --- 페이지 렌더링 -------------------------------------------------------------
def render_pages(pdf: Path, page_dirs: list[Path]) -> list[Path]:
    """PDF 각 페이지를 PNG로 렌더링(이미 있으면 스킵). 반환: 페이지 이미지 경로 리스트."""
    imgs = [d / "page.png" for d in page_dirs]
    if all(p.exists() for p in imgs):
        return imgs
    images = convert_from_path(str(pdf), dpi=DPI, last_page=len(page_dirs))
    out = []
    for i, img in enumerate(images[:len(page_dirs)]):
        page_dirs[i].mkdir(parents=True, exist_ok=True)
        dst = page_dirs[i] / "page.png"
        img.save(dst, "PNG")
        out.append(dst)
    return out


# --- Ollama 호출 ---------------------------------------------------------------
def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def call_ollama(prompt: str, image_path: Path, *, fmt: str | None = None, num_ctx: int = 8192) -> str:
    """qwen2.5vl 호출(로컬·오프라인). fmt='json'이면 유효 JSON 강제."""
    import urllib.request
    payload = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt, "images": [_b64(image_path)]}],
        "stream": False,
        "options": {"temperature": 0, "num_ctx": num_ctx},
    }
    if fmt:
        payload["format"] = fmt
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{OLLAMA_HOST}/api/chat", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return (body.get("message") or {}).get("content", "")


# --- 파서 1: OCR (Tesseract) ---------------------------------------------------
def run_ocr(image_path: Path) -> str:
    return pytesseract.image_to_string(Image.open(image_path), lang="eng").strip()


# --- 파서 2: QWEN 충실 전사 ----------------------------------------------------
TRANSCRIBE_PROMPT = """이 페이지 이미지를 있는 그대로 Markdown으로 정확히 전사하세요.
규칙:
- 페이지의 모든 본문 텍스트를 빠짐없이 포함하세요. 요약·생략·의역 금지.
- 제목/소제목은 #, ## 로. 표는 반드시 Markdown 표로, 행과 열을 정확히 유지.
- 목록은 - 또는 번호로. 원문에 없는 내용은 절대 지어내지 마세요.
- 그림은 ![](image) 로 표시하고 캡션이 있으면 그대로 옮기세요.
- 설명 문장 없이 전사 결과(Markdown)만 출력하세요."""


def run_qwen(image_path: Path) -> str:
    raw = call_ollama(TRANSCRIBE_PROMPT, image_path, num_ctx=8192)
    return re.sub(r"^```(?:markdown)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()


# --- 파서 3: MinerU 2.5 --------------------------------------------------------
def run_mineru_doc(pdf: Path, doc_dir: Path) -> dict[int, str]:
    """MinerU로 문서를 파싱(content_list.json 없으면 CLI 실행)하고 page_idx(0-based)→markdown 반환."""
    mineru_root = doc_dir / "_mineru"
    content_list = next(mineru_root.rglob("*_content_list.json"), None) if mineru_root.exists() else None
    if content_list is None:
        mineru_root.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "MINERU_MODEL_SOURCE": os.environ.get("MINERU_MODEL_SOURCE", "huggingface")}
        exe = str(Path(sys.executable).parent / "mineru.exe")
        log.info(f"MinerU 실행: {pdf.name}")
        subprocess.run([exe, "-p", str(pdf), "-o", str(mineru_root), "-b", "vlm-engine"],
                       check=True, env=env)
        content_list = next(mineru_root.rglob("*_content_list.json"))
    blocks = json.loads(Path(content_list).read_text(encoding="utf-8"))
    return mineru_blocks_to_pages(blocks)


def _join(v) -> str:
    """MinerU 캡션/각주는 list 또는 str로 옴 — 공백으로 합쳐 문자열로."""
    return " ".join(v).strip() if isinstance(v, list) else (v or "").strip()


def mineru_blocks_to_pages(blocks: list[dict]) -> dict[int, str]:
    pages: dict[int, list[str]] = defaultdict(list)
    for b in blocks:
        idx = b.get("page_idx", 0)
        t = b.get("type")
        if t == "text":
            txt = (b.get("text") or "").strip()
            if not txt:
                continue
            lvl = b.get("text_level")
            pages[idx].append(f"{'#' * lvl} {txt}" if lvl else txt)
        elif t == "list":
            items = b.get("list_items") or []
            if items:
                pages[idx].extend(f"- {it}" for it in items)
            elif b.get("text"):
                pages[idx].append(b["text"].strip())
        elif t == "table":
            # 표 제목(table_caption)·본문(table_body)·각주(table_footnote)를 모두 포함
            parts = [_join(b.get("table_caption")),
                     (b.get("table_body") or b.get("content") or "").strip(),
                     _join(b.get("table_footnote"))]
            block = "\n".join(p for p in parts if p)
            if block:
                pages[idx].append(block)
        elif t == "image":
            cap = _join(b.get("image_caption"))
            pages[idx].append(f"![]({b.get('img_path','')}) {cap}".strip())
        else:
            # header · page_number · footer · equation 등 그 외 타입도 text가 있으면 포함(누락 방지)
            txt = (b.get("text") or "").strip()
            if txt:
                pages[idx].append(txt)
    return {idx: "\n\n".join(v).strip() for idx, v in pages.items()}


# --- 심판(VLM) -----------------------------------------------------------------
JUDGE_PROMPT_TMPL = """아래는 한 페이지의 원본 이미지와, 이 페이지를 텍스트로 '전사'한 결과 3개(A/B/C)입니다.
각 결과가 원본 페이지 내용을 얼마나 정확히 담았는지 채점하세요. 도구 이름은 숨겨져 있습니다.

채점 기준(각 정수 0~5, 5가 최고):
- fidelity: 텍스트 정확도(오탈자/오인식 없이 원문과 일치)
- completeness: 원본의 내용을 누락 없이 모두 포함(빠진 문단/셀이 없음)
- structure: 제목·표·목록 등 구조 보존(특히 표의 행/열이 원본과 일치)

반드시 아래 JSON만 출력(설명 금지):
{{"A":{{"fidelity":0,"completeness":0,"structure":0,"note":"근거 한 문장"}},
 "B":{{"fidelity":0,"completeness":0,"structure":0,"note":"..."}},
 "C":{{"fidelity":0,"completeness":0,"structure":0,"note":"..."}},
 "best":"A","reason":"가장 정확한 것과 이유 한 문장"}}

=== A ===
{A}

=== B ===
{B}

=== C ===
{C}
"""


def judge_page(image_path: Path, texts: dict[str, str], seed: int) -> tuple[dict, dict]:
    """블라인드 채점. 반환: (파싱된 심판 JSON(라벨 A/B/C 기준), 라벨→도구 매핑)."""
    tools = list(TOOLS)
    random.Random(seed).shuffle(tools)
    label_to_tool = dict(zip(("A", "B", "C"), tools))
    filled = JUDGE_PROMPT_TMPL.format(
        A=(texts[label_to_tool["A"]] or "(빈 출력)")[:JUDGE_CHAR_CAP],
        B=(texts[label_to_tool["B"]] or "(빈 출력)")[:JUDGE_CHAR_CAP],
        C=(texts[label_to_tool["C"]] or "(빈 출력)")[:JUDGE_CHAR_CAP],
    )
    raw = call_ollama(filled, image_path, fmt="json", num_ctx=16384)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        parsed = json.loads(m.group(0)) if m else {}
    return parsed, label_to_tool


# --- 메인 파이프라인 -----------------------------------------------------------
def process_doc(pdf: Path, max_pages: int | None, force: bool, no_judge: bool = False) -> list[dict]:
    doc_dir = EXP_OUT / pdf.stem
    doc_dir.mkdir(parents=True, exist_ok=True)

    n_pages = len(convert_from_path(str(pdf), dpi=72))  # 페이지 수만 가볍게
    if max_pages:
        n_pages = min(n_pages, max_pages)
    page_dirs = [doc_dir / f"p{i + 1:02d}" for i in range(n_pages)]
    for d in page_dirs:
        d.mkdir(parents=True, exist_ok=True)

    log.info(f"[{pdf.name}] {n_pages}페이지 — 렌더링")
    imgs = render_pages(pdf, page_dirs)[:n_pages]

    log.info(f"[{pdf.name}] MinerU 파싱")
    mineru_pages = run_mineru_doc(pdf, doc_dir)  # page_idx(0-based) → md

    rows = []
    for i, (pdir, img) in enumerate(zip(page_dirs, imgs)):
        # 1) OCR
        ocr_path = pdir / "ocr.txt"
        if force or not ocr_path.exists():
            ocr_path.write_text(run_ocr(img), encoding="utf-8")
        # 2) QWEN
        qwen_path = pdir / "qwen.md"
        if force or not qwen_path.exists():
            log.info(f"[{pdf.name}] p{i+1} QWEN 전사")
            qwen_path.write_text(run_qwen(img), encoding="utf-8")
        # 3) MinerU (페이지 슬라이스 저장)
        mineru_path = pdir / "mineru.md"
        if force or not mineru_path.exists():
            mineru_path.write_text(mineru_pages.get(i, ""), encoding="utf-8")

        texts = {
            "ocr": ocr_path.read_text(encoding="utf-8"),
            "qwen": qwen_path.read_text(encoding="utf-8"),
            "mineru": mineru_path.read_text(encoding="utf-8"),
        }

        # 4) 심판 채점 (--no-judge면 스킵 — Claude가 직접 채점)
        judged = None
        if not no_judge:
            judge_path = pdir / "judge.json"
            if force or not judge_path.exists():
                log.info(f"[{pdf.name}] p{i+1} 심판 채점")
                parsed, mapping = judge_page(img, texts, seed=RNG_SEED + i)
                judge_path.write_text(json.dumps(
                    {"scores_by_label": parsed, "label_to_tool": mapping}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            judged = json.loads(judge_path.read_text(encoding="utf-8"))

        rows.append({
            "doc": pdf.stem, "page": i + 1,
            "chars": {t: len(texts[t]) for t in TOOLS},
            "judge": judged,
        })
    return rows


def aggregate(all_rows: list[dict]) -> dict:
    """도구별 점수 집계(전체 + 문서별). 라벨 점수를 도구로 역매핑."""
    METRICS = ("fidelity", "completeness", "structure")
    acc = {t: {m: [] for m in METRICS} for t in TOOLS}
    per_doc = defaultdict(lambda: {t: {m: [] for m in METRICS} for t in TOOLS})
    wins = {t: 0 for t in TOOLS}
    for r in all_rows:
        sbl = r["judge"].get("scores_by_label", {})
        mapping = r["judge"].get("label_to_tool", {})
        for label, tool in mapping.items():
            s = sbl.get(label, {})
            for m in METRICS:
                v = s.get(m)
                if isinstance(v, (int, float)):
                    acc[tool][m].append(v)
                    per_doc[r["doc"]][tool][m].append(v)
        best_label = sbl.get("best")
        if best_label in mapping:
            wins[mapping[best_label]] += 1

    def _avg(d):
        return {t: {m: (round(sum(d[t][m]) / len(d[t][m]), 2) if d[t][m] else None) for m in METRICS}
                for t in TOOLS}

    overall = _avg(acc)
    for t in TOOLS:
        vals = [overall[t][m] for m in METRICS if overall[t][m] is not None]
        overall[t]["total"] = round(sum(vals) / len(vals), 2) if vals else None
    return {"overall": overall, "per_doc": {d: _avg(v) for d, v in per_doc.items()},
            "best_page_wins": wins, "n_pages": len(all_rows)}


def build_report(all_rows: list[dict], agg: dict) -> str:
    METRICS = ("fidelity", "completeness", "structure")
    L = []
    L.append("# 문서 파서 정확도 비교: OCR vs QWEN(qwen2.5vl) vs MinerU 2.5\n")
    L.append(f"- 대상: {', '.join(sorted({r['doc'] for r in all_rows}))}")
    L.append(f"- 총 {agg['n_pages']}페이지 · 동일 과제(페이지 충실 전사) · 블라인드 VLM 심판(qwen2.5vl)")
    L.append("- 점수: 0~5 (fidelity 텍스트정확도 · completeness 누락없음 · structure 구조/표보존)\n")

    L.append("## 종합 점수 (전체 평균)\n")
    L.append("| 파서 | fidelity | completeness | structure | **평균** | best-page 승 |")
    L.append("|---|---|---|---|---|---|")
    label = {"ocr": "OCR (Tesseract)", "qwen": "QWEN (qwen2.5vl 전사)", "mineru": "MinerU 2.5"}
    for t in TOOLS:
        o = agg["overall"][t]
        L.append(f"| {label[t]} | {o['fidelity']} | {o['completeness']} | {o['structure']} "
                 f"| **{o['total']}** | {agg['best_page_wins'][t]} |")
    L.append("")

    L.append("## 문서별 평균\n")
    for doc, per in agg["per_doc"].items():
        L.append(f"### {doc}\n")
        L.append("| 파서 | fidelity | completeness | structure |")
        L.append("|---|---|---|---|")
        for t in TOOLS:
            p = per[t]
            L.append(f"| {label[t]} | {p['fidelity']} | {p['completeness']} | {p['structure']} |")
        L.append("")

    L.append("## 페이지별 심판 점수 (도구 역매핑 후)\n")
    L.append("| 문서 | p | OCR (f/c/s) | QWEN (f/c/s) | MinerU (f/c/s) | best |")
    L.append("|---|---|---|---|---|---|")
    for r in all_rows:
        sbl = r["judge"].get("scores_by_label", {})
        mp = r["judge"].get("label_to_tool", {})
        tool_to_label = {v: k for k, v in mp.items()}
        def fcs(t):
            s = sbl.get(tool_to_label.get(t, ""), {})
            return "/".join(str(s.get(m, "·")) for m in METRICS)
        best = mp.get(sbl.get("best"), "?")
        L.append(f"| {r['doc']} | {r['page']} | {fcs('ocr')} | {fcs('qwen')} | {fcs('mineru')} | {best} |")
    L.append("")

    L.append("## 참고: 산출물 위치\n")
    L.append("- 페이지별 원문·전사·심판: `exp_out/<doc>/p<NN>/{page.png, ocr.txt, qwen.md, mineru.md, judge.json}`")
    L.append("- 집계 점수: `exp_out/scores.json`\n")
    L.append("> ⚠️ 한계: 심판이 후보 중 하나(QWEN)와 동일 모델이라 자기편향 가능. "
             "블라인드(A/B/C 무작위)로 완화했으나, `qwen.md`/`mineru.md`/`ocr.txt`를 직접 비교해 교차검증 권장.")
    return "\n".join(L)


def build_parse_index(all_rows: list[dict]) -> str:
    """--no-judge 모드: 파싱 산출물 인덱스 + 문자 커버리지만. 채점은 별도(Claude)."""
    L = ["# 파서 산출물 인덱스 (채점 전) — OCR vs QWEN vs MinerU 2.5\n",
         f"- 총 {len(all_rows)}페이지 · 각 페이지에 page.png / ocr.txt / qwen.md / mineru.md 생성",
         "- 아래 문자수는 참고용(정확도 아님). 실제 정확도는 Claude가 페이지 이미지와 대조해 채점.\n",
         "| 문서 | p | OCR 자 | QWEN 자 | MinerU 자 |",
         "|---|---|---|---|---|"]
    for r in all_rows:
        c = r["chars"]
        L.append(f"| {r['doc']} | {r['page']} | {c['ocr']} | {c['qwen']} | {c['mineru']} |")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=None, help="문서당 페이지 제한(테스트용)")
    ap.add_argument("--force", action="store_true", help="캐시 무시하고 전 단계 재실행")
    ap.add_argument("--no-judge", action="store_true", help="자동 심판 스킵(파싱 산출물만 — Claude가 직접 채점)")
    args = ap.parse_args()

    EXP_OUT.mkdir(parents=True, exist_ok=True)
    all_rows = []
    for pdf in DOCS:
        if not pdf.exists():
            log.warning(f"문서 없음, 스킵: {pdf}")
            continue
        all_rows.extend(process_doc(pdf, args.max_pages, args.force, no_judge=args.no_judge))

    if args.no_judge:
        (EXP_OUT / "parse_index.md").write_text(build_parse_index(all_rows), encoding="utf-8")
        log.info(f"파싱 완료(채점 전) — 인덱스: {EXP_OUT / 'parse_index.md'} · 페이지 {len(all_rows)}개")
        return

    agg = aggregate(all_rows)
    (EXP_OUT / "scores.json").write_text(
        json.dumps({"aggregate": agg, "rows": all_rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    report = build_report(all_rows, agg)
    (EXP_OUT / "report.md").write_text(report, encoding="utf-8")
    log.info(f"완료 — 리포트: {EXP_OUT / 'report.md'}")
    print("\n" + report)


if __name__ == "__main__":
    main()
