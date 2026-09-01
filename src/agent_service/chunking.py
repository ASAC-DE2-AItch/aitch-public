# -*- coding: utf-8 -*-
"""
error_manual 청커 — MinerU 파싱본(content_list.json) → kb_schema.yaml 공통 payload jsonl.

입력 : script/mineru_pipeline/kb_parse/kb_out/<문서>/<auto|vlm>/<문서>_content_list.json
출력 : rag_kb/error_manual/error_manual.jsonl  (kb_type='error_manual', 공통 payload 14필드)
정책 :
  · 이미지 블록·이미지캡션 노이즈("no visible text" 류)·footer·page_number 제거
  · 섹션 heading을 청크 앞에 붙여 맥락 보존, ~CHUNK_CHARS 단위로 분할
  · 중복 문서(oxford_plasmalab80 == Oxford_Plasmalab_80_Plus)는 본문 해시로 dedup
  · DRAM(process_knowledge)은 이미지 위주라 제외 — VLM 재캡셔닝 후 별도 적재(추후)
  · error_manual 전용 payload(error_code/symptom/cause/corrective_action...)는 구조화 추출
    별도 단계 — 본 1차 청크는 공통 payload + text(임베딩 대상)만 채운다.

실행 : python -m src.agent_service.chunking   (stdlib만 사용, 모델·DB 불필요)
"""
import argparse
import hashlib
import json
import logging
import re
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ [chunking] %(message)s")
log = logging.getLogger("chunking")

BASE = Path(__file__).resolve().parent
KB_OUT = BASE / "script" / "mineru_pipeline" / "kb_parse" / "kb_out"
OUT_PATH = BASE / "rag_kb" / "error_manual" / "error_manual.jsonl"

CHUNK_CHARS = 900
MIN_CHARS = 100
DROP_TYPES = {"image", "footer", "page_number", "page_footnote", "page_footer", "header_footer"}

# 문서 slug → (표시 제목, equipment 필터값). error_manual 매뉴얼 7종 (dram_* 제외).
DOC_META = {
    "oxford_100_manual":            ("Oxford PlasmaLab System 100 (ICP) Manual", "Oxford_100"),
    "operation-spec-oxford-system-100-rev-c": ("Oxford System 100 Operation Spec", "Oxford_100"),
    "oxford_deep_reactive_ion_etch-plasmalab_system_100": ("Oxford PlasmaLab 100 DRIE", "PlasmaLab_100"),
    "oxford_plasmalab_80_plus_rie_operation_training":    ("Oxford PlasmaLab 80+ RIE Training", "Plasmalab_80"),
    "oxford_plasmalab80":           ("Oxford PlasmaLab 80+ RIE Operation", "Plasmalab_80"),
    "oxford_sop":                   ("UMN NFC Oxford Etcher SOP", "Oxford_180ICP"),
    "plasmatherm_790":              ("PlasmaTherm 790 Operation", "PlasmaTherm_790"),
    "drie_sop_-_august_2023":       ("DRIE SOP (Aug 2023)", "DRIE"),
}

# 이미지 캡션·장식 노이즈 (VLM이 그림을 서술한 것 — 지식 아님)
_NOISE_START = ("this image", "close-up", "portrait", "industrial", "a man", "an image",
                "a metallic", "a close", "four ", "black cyl", "a photograph", "a diagram of a")
_NOISE_RE = re.compile(r"no (visible )?text|no (visible )?symbol|no discernible|blank image", re.I)
_ESCAPE_RE = re.compile(r"\\([-~*_#])")       # \-, \~ 등 마크다운 이스케이프 잔재
_WS_RE = re.compile(r"[ \t]+")
_LEADER_RE = re.compile(r"\.{2,}|…")          # 목차 점선 리더 (.. ....  …)
# 지식 아님 — 표지·양식·연락처 보일러플레이트 (정비 지식 없음)
_ADMIN_RE = re.compile(r"GOODS RETURN|Returns Authorisation|Quality Control Form|"
                       r"Works Order No|Customer Support Facilities", re.I)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.M)  # 불릿 마커
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")       # **볼드**
# 매뉴얼 절번호 제거 — 측정값(2.45 GHz, 0.5 sccm)은 보존해야 하므로 신중.
_UNITS = ("GHz|MHz|kHz|Hz|sccm|slm|mbar|mTorr|Torr|bar|Pa|nm|µm|um|mm|cm|"
          "kW|mW|kV|mV|kg|mg|ms|min|sec|hr|°C|℃|%|W|V|A|K|s|h")
# 3~4파트(1.3.1 / 1.1.1)는 측정값과 안 겹침 — 줄머리·문장경계에서 제거
_SECNUM3 = re.compile(r"(?m)(?:^|(?<=[.)\]] ))\d{1,2}(?:\.\d{1,2}){2,3}\s+(?=[A-Za-z가-힣])")
# 2파트(1.1 Xxx)는 줄머리 + 다음 토큰이 단위가 아닐 때만 (측정값 오제거 방지)
_SECNUM2 = re.compile(rf"(?m)^\s*\d{{1,2}}\.\d{{1,2}}\s+(?!(?:{_UNITS})\b)(?=[A-Za-z가-힣])")


def _clean(text: str) -> str:
    """마크다운 이스케이프·불릿·볼드 + 매뉴얼 절번호 제거, 공백 정규화 (측정값은 보존)."""
    text = _ESCAPE_RE.sub(r"\1", text or "")
    text = _BOLD_RE.sub(r"\1", text).replace("**", "")
    text = _BULLET_RE.sub("", text)
    text = _SECNUM3.sub("", text)
    text = _SECNUM2.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def _is_noise(text: str) -> bool:
    """이미지 캡션·장식 서술이면 True (본문 지식이 아님)."""
    t = text.strip().lower()
    if not t:
        return True
    if _NOISE_RE.search(t):
        return True
    if t.startswith(_NOISE_START) and len(t) < 200:
        return True
    return False


def _is_toc(text: str) -> bool:
    """목차(TOC) 페이지 청크면 True — 점선 리더(.. …)가 4회 이상이면 목차 잔재로 본다."""
    return len(_LEADER_RE.findall(text)) >= 4


def _is_admin(text: str) -> bool:
    """표지·반품양식·연락처 보일러플레이트면 True (지식 아님 → 드롭)."""
    return bool(_ADMIN_RE.search(text))


def _iter_docs():
    """kb_out 아래 error_manual 대상 문서(폴더)를 순회한다 (dram_* 제외, 문서당 content_list 1개)."""
    for doc_dir in sorted(KB_OUT.iterdir()):
        if not doc_dir.is_dir() or doc_dir.name.lower().startswith("dram"):
            continue
        cls = sorted(doc_dir.glob("*/*_content_list.json"))
        if cls:
            yield doc_dir.name, cls[0]


def _chunks_from_blocks(blocks):
    """블록 리스트 → (heading, body_text, page_min, page_max) 청크들을 생성한다."""
    heading = ""
    buf, pages = [], []

    def flush():
        body = _clean(" ".join(buf))
        full = f"{heading} {body}"   # heading에만 있는 양식/목차 표식도 잡도록 함께 검사
        if len(body) >= MIN_CHARS and not _is_toc(full) and not _is_admin(full):  # 목차·양식 드롭
            yield heading, body, (min(pages) if pages else None), (max(pages) if pages else None)

    for b in blocks:
        btype = b.get("type")
        if btype in DROP_TYPES:
            continue
        raw = b.get("text")
        if isinstance(raw, list):
            raw = " ".join(map(str, raw))
        text = _clean(raw or "")
        if not text or _is_noise(text):
            continue
        pidx = b.get("page_idx")
        # heading(섹션 제목) 감지 → 섹션 경계에서 flush
        if btype in ("header", "title") or b.get("text_level"):
            if buf:
                yield from flush()
                buf, pages = [], []
            heading = text[:120]
            continue
        buf.append(text)
        if pidx is not None:
            pages.append(pidx)
        if sum(len(x) for x in buf) >= CHUNK_CHARS:
            yield from flush()
            buf, pages = [], []
    if buf:
        yield from flush()


def build():
    """error_manual 매뉴얼 전체를 청킹해 kb_schema 공통 payload 레코드 리스트를 만든다."""
    records, seen_hashes = [], set()
    for folder, cl_path in _iter_docs():
        slug = folder.lower()
        title, equipment = DOC_META.get(slug, (folder, None))
        blocks = json.loads(cl_path.read_text(encoding="utf-8"))
        doc_chunks = list(_chunks_from_blocks(blocks))
        # 문서 dedup: 본문 합쳐 해시 (oxford_plasmalab80 중복 스킵)
        doc_hash = hashlib.md5("".join(c[1] for c in doc_chunks).encode("utf-8")).hexdigest()
        if doc_hash in seen_hashes:
            log.info(f"중복 문서 스킵: {folder}")
            continue
        seen_hashes.add(doc_hash)

        doc_id = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
        for i, (heading, body, pmin, pmax) in enumerate(doc_chunks, 1):
            # text=본문만. 문서명(title)은 title 필드가 보유 → 중복 제거.
            # 섹션 heading은 별도 필드가 없으므로 맥락 보존 위해 본문 앞에 유지.
            text = f"{heading}\n{body}" if heading else body
            page = None
            if pmin is not None:
                page = f"p{pmin + 1}" if pmin == pmax else f"p{pmin + 1}-p{pmax + 1}"
            records.append({
                # 공통 payload 12 (kb_schema.yaml) — chamber·rule_id 삭제(2026-07-10 상위법 정렬)
                "doc_id": doc_id,
                "chunk_id": f"{doc_id}#c{i:04d}",
                "kb_type": "error_manual",
                "text": text,
                "title": title,
                "source": f"MinerU:{folder}",
                "section_page": page,
                "equipment": equipment,
                "sensor": None,
                "pm_proximity": None,
                "provenance": "manual",
                "created_at": None,
            })
        log.info(f"{folder}: {len(doc_chunks)}청크 (equipment={equipment})")
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", default=str(OUT_PATH))
    args = ap.parse_args()
    records = build()
    out = Path(args.o)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    log.info(f"총 {len(records)}청크 → {out}")
    log.info(f"문서별: {dict(Counter(r['doc_id'] for r in records))}")


if __name__ == "__main__":
    main()
