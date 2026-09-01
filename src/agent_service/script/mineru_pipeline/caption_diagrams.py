#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DRAM 슬라이드 다이어그램 캡셔닝 — MinerU 파싱 산출물의 이미지에 기술 설명을 붙인다.

배경: DRAM 교육 슬라이드는 소자 구조도·공정 다이어그램에 핵심 지식이 '그림'으로 들어있는데,
MinerU pipeline 백엔드는 그림을 파일로 잘라내기만 하고 설명을 안 붙인다(vlm도 절반만).
→ 로컬 비전 LLM(qwen2.5vl)으로 각 다이어그램을 기술적으로 설명해 KB 텍스트에 주입한다.

동작:
  kb_out/<doc>/{vlm|auto}/<doc>_content_list.json 을 읽어, image/chart 블록 중
  이미지 파일이 THRESHOLD(기본 30KB) 이상인 것만 qwen2.5vl로 캡션 생성 →
    - <doc>_content_list_captioned.json : 각 블록에 llm_caption / caption_provenance 추가
    - <doc>_captioned.md               : 그림 아래에 '> 📊 그림 설명: ...' 삽입
    - <doc>_caption_cache.json         : {이미지파일명: 캡션} (재시작 시 재사용)

환각 방지: 약어를 임의로 풀어쓰지 말고, 그림에 없는 내용/수치를 지어내지 말도록 프롬프트로 강제.
로고·표지·장식 이미지는 모델이 '설명 대상 아님'으로 스킵.

실행: C:\\aitch-mineru-venv\\Scripts\\python.exe caption_diagrams.py [--threshold-kb 30] [--limit N] [--doc NAME]
사전조건: Ollama(qwen2.5vl:7b) 기동.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import logging
import os
import sys
import time
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image

for _s in (sys.stdout, sys.stderr):
    if _s.encoding and _s.encoding.lower() != "utf-8":
        _s.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ [Caption] %(message)s")
log = logging.getLogger("caption")

KB_OUT = Path(__file__).resolve().parent / "kb_parse" / "kb_out"
DRAM_DOCS = ["dram_device_physics", "dram_jikmu_process", "dram_soja_ihae",
             "dram_dongjak_wonri", "dram_process_integration"]

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5vl:7b")
MAX_ATTEMPTS = 3
SKIP_MARK = "설명 대상 아님"

def build_prompt(context: str = "") -> str:
    ctx = ""
    if context.strip():
        ctx = ("\n[같은 슬라이드에서 추출된 텍스트 — 용어·약어의 정확한 표기 참고용]\n"
               f"{context.strip()[:900]}\n")
    return (
        "당신은 반도체 DRAM 공정·소자 전문가입니다. 아래는 DRAM 교육자료 슬라이드에서 추출한 그림/다이어그램입니다."
        f"{ctx}\n"
        "이 그림이 기술적으로 무엇을 나타내는지 한국어로 2~4문장으로 설명하세요.\n"
        "규칙:\n"
        "1) 그림에 실제로 보이는 구성요소·구조·신호/전하 흐름·공정 단계만 서술하세요.\n"
        "2) 약어(WL, BL, TR, Cap, SA, Vt, VDS, VGB 등)는 **절대 풀어쓰지 말고 원문 표기 그대로** 쓰세요. "
        "예를 들어 'VGB'를 'Barrier Height'로, 'PG'를 'Page Gate'로 임의 확장하지 마세요. 모르면 약어 그대로 두세요.\n"
        "3) 그림에 없는 내용·수치를 지어내지 마세요. 위 '같은 슬라이드 텍스트'에 나온 용어를 우선 사용하세요.\n"
        f"4) 그림이 단순 로고·표지·인물사진·장식이면 다른 말 없이 '{SKIP_MARK}'만 출력하세요.\n"
        "설명만 출력하세요."
    )


MAX_SIDE = 1536  # 초고해상도 이미지는 비전 토큰 과다·타임아웃 유발 → 긴 변을 이 값으로 다운스케일


def _image_b64(img_path: Path) -> str:
    """이미지를 base64로. 긴 변이 MAX_SIDE를 넘으면 비율 유지 축소(PNG 재인코딩)."""
    raw = img_path.read_bytes()
    try:
        im = Image.open(BytesIO(raw))
        if max(im.size) > MAX_SIDE:
            im = im.convert("RGB")
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            buf = BytesIO()
            im.save(buf, format="PNG")
            raw = buf.getvalue()
    except Exception:
        pass  # 열기 실패 시 원본 그대로 시도
    return base64.b64encode(raw).decode("ascii")


def ollama_caption(img_path: Path, context: str = "") -> str | None:
    """이미지 1장을 qwen2.5vl로 캡션. context=같은 슬라이드 텍스트(용어 근거). 실패 시 재시도, 전부 실패하면 None."""
    b64 = _image_b64(img_path)
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": build_prompt(context), "images": [b64]}],
        "stream": False,
        "options": {"temperature": 0, "num_ctx": 8192},
    }
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(f"{OLLAMA_HOST}/api/chat", data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                body = json.loads(r.read().decode("utf-8"))
            return ((body.get("message") or {}).get("content") or "").strip()
        except Exception as e:
            log.warning(f"{img_path.name}: 호출 오류(시도 {attempt}/{MAX_ATTEMPTS}) {e}")
            time.sleep(2)
    return None


# qwen이 기술용어를 이따금 중국어(한자)로 출력 → 한글/영문으로 치환(CJK 누출 제거).
# 여러 글자 먼저 치환되도록 긴 키부터 정렬해 적용한다.
_CJK_REPL = {
    "蚀刻": "식각", "顯微鏡": "현미경", "顯微镜": "현미경", "数值孔径": "개구수(NA)",
    "饱和区域": "포화영역", "饱和电压": "포화전압", "饱和电流": "포화전류",
    "閾值電壓": "임계전압", "閾值電圧": "임계전압", "费米能级": "페르미 준위",
    "挥发性": "휘발성", "理想": "이상적", "再到": "다시", "转变": "전환", "饱和": "포화",
    "ホール": "홀(hole)",  # 일본어 가타카나 'hole'(정공) 누출
    "胞": "셀", "帶": "밴드", "带": "밴드", "場": "장", "场": "장", "子": "자", "時": "시",
}


def _scrub_cjk(text: str) -> str:
    for k in sorted(_CJK_REPL, key=len, reverse=True):
        text = text.replace(k, _CJK_REPL[k])
    return text


def _blocks_to_captioned_md(blocks: list[dict]) -> str:
    """content_list 블록을 순서대로 Markdown으로 — 이미지 아래 llm_caption을 인용구로 삽입."""
    out = []
    for b in blocks:
        t = b.get("type")
        if t == "text":
            txt = (b.get("text") or "").strip()
            if not txt:
                continue
            lvl = b.get("text_level")
            out.append(f"{'#' * lvl} {txt}" if lvl else txt)
        elif t == "list":
            items = b.get("list_items") or []
            if items:
                out.extend(f"- {it}" for it in items)
            elif b.get("text"):
                out.append(b["text"].strip())
        elif t == "table":
            cap = b.get("table_caption")
            cap = " ".join(cap) if isinstance(cap, list) else (cap or "")
            body = b.get("table_body") or b.get("content") or ""
            out.append("\n".join(x for x in (cap.strip(), body.strip()) if x))
        elif t in ("image", "chart"):
            out.append(f"![]({b.get('img_path','')})")
            llm = b.get("llm_caption")
            if llm and llm != SKIP_MARK:
                out.append(f"> 📊 그림 설명: {llm}")
        else:
            txt = (b.get("text") or "").strip()
            if txt:
                out.append(txt)
    return _scrub_cjk("\n\n".join(out).strip())  # 표 등 원본 콘텐츠의 잔존 한자까지 최종 정리


def process_doc(doc: str, threshold: int, limit: int | None) -> dict:
    cls = glob.glob(str(KB_OUT / doc / "**" / f"{doc}_content_list.json"), recursive=True)
    if not cls:
        log.warning(f"{doc}: content_list 없음, 스킵")
        return {}
    cl_path = Path(cls[0])
    base = cl_path.parent
    blocks = json.loads(cl_path.read_text(encoding="utf-8"))

    cache_path = base / f"{doc}_caption_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    # 페이지별 텍스트 문맥 — 같은 슬라이드의 text/list/title 블록을 모아 캡션 근거로 제공(약어 정확도↑)
    page_text: dict[int, list[str]] = {}
    for b in blocks:
        if b.get("type") == "text" and (b.get("text") or "").strip():
            page_text.setdefault(b.get("page_idx", 0), []).append(b["text"].strip())
        elif b.get("type") == "list" and b.get("list_items"):
            page_text.setdefault(b.get("page_idx", 0), []).extend(str(x) for x in b["list_items"])
    page_ctx = {p: "\n".join(v) for p, v in page_text.items()}

    targets = []
    for b in blocks:
        if b.get("type") not in ("image", "chart"):
            continue
        ip = b.get("img_path")
        if not ip:
            continue
        f = base / ip
        if f.exists() and f.stat().st_size >= threshold:
            targets.append((b, f))
    if limit:
        targets = targets[:limit]

    log.info(f"{doc}: 캡션 대상 {len(targets)}장 (>{threshold//1024}KB), 캐시 {len(cache)}건")
    done = skipped = failed = 0
    for i, (b, f) in enumerate(targets, 1):
        key = f.name
        if key in cache:
            cap = cache[key]
        else:
            cap = ollama_caption(f, context=page_ctx.get(b.get("page_idx", 0), ""))
            if cap is None:
                failed += 1
                continue
            cache[key] = cap
            if i % 10 == 0:  # 주기적 캐시 저장 (긴 작업 중 진행 보존)
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
                log.info(f"{doc}: {i}/{len(targets)} 진행")
        cap = _scrub_cjk(cap)  # 한자 누출 제거(한글/영문 치환)
        b["llm_caption"] = cap
        b["caption_provenance"] = f"{MODEL}:diagram_caption"
        if cap == SKIP_MARK:
            skipped += 1
        else:
            done += 1

    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    (base / f"{doc}_content_list_captioned.json").write_text(
        json.dumps(blocks, ensure_ascii=False, indent=1), encoding="utf-8")
    (base / f"{doc}_captioned.md").write_text(_blocks_to_captioned_md(blocks), encoding="utf-8")
    log.info(f"{doc}: 캡션 {done} · 스킵(비다이어그램) {skipped} · 실패 {failed} → *_captioned.md/json 저장")
    return {"doc": doc, "captioned": done, "skipped": skipped, "failed": failed, "targets": len(targets)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold-kb", type=int, default=30, help="이 크기 이상 이미지만 캡션 (기본 30KB)")
    ap.add_argument("--limit", type=int, default=None, help="문서당 캡션 개수 제한(테스트용)")
    ap.add_argument("--doc", default=None, help="특정 문서만 (기본: DRAM 5개 전부)")
    args = ap.parse_args()

    docs = [args.doc] if args.doc else DRAM_DOCS
    stats = []
    for doc in docs:
        stats.append(process_doc(doc, args.threshold_kb * 1024, args.limit))
    log.info("=== 전체 요약 ===")
    for s in stats:
        if s:
            log.info(f"  {s['doc']}: 캡션 {s['captioned']}/{s['targets']} (스킵 {s['skipped']}, 실패 {s['failed']})")


if __name__ == "__main__":
    main()
