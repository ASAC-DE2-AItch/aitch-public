# -*- coding: utf-8 -*-
"""근거의 질(③) · 오답 방향(②) **사후 분석기** — 저장본 재계산 (LLM 재호출 없음).

eval_supervisor.py 가 남긴 supervisor_results_<tag>.jsonl 을 읽어, 지금까지 **손으로 세던**
지표 두 축을 코드로 재현한다. EC2·LLM 을 부르지 않으므로 쿼터 0 이어도 언제든 돈다.

두 축 (시작메모 §4):
    ② 오답의 방향  — 틀렸을 때 **어디로** 틀렸나 (되돌릴 수 있는 쪽인가)
    ③ 근거의 질    — **맞아도** 제대로 맞았나 (정답 옵션이 확신 1위였나·카드는 있었나)

⚠️ 이건 계약/빌드 도구가 아니라 **실험 계측**이다. eval_supervisor.py 의 채점(①)을
건드리지 않는다(그 하네스는 CI 게이트라 안전해야 함). 검증되면 score() 에 접거나
멘토 확인(②는 "🔷 멘토 확인 후 구현" 상태) 후 정식 편입한다.

사용:
    python -m src.agent_service.analyze_quality --tag round8
    python -m src.agent_service.analyze_quality --path notes/eval/supervisor_results_round8.jsonl

정합 검증(2026-07-23): R8 손측정 = 정답옵션 conf 1위 아님 10/22 · 카드0장 0/22 · manual 매뉴얼근거 21/22.
③-C 재정의(2026-07-24): 구 지표는 `[KB` 문자열 유무만 봐 **건강한 리포트를 병으로 셌다**
    (출처 태그는 필수라 항상 있음 — fewshot3 25/25 는 오탐). "정비를 제안했는데 변별 신호를
    못 댔나"로 교체. 같은 라운드 재측정 시 값이 크게 낮아지는 것이 정상이다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

SERVICE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_ROOT.parents[1]))

from src.agent_service.app.schemas.report import VERDICT_TO_OPTION  # noqa: E402

# --- ② 가역성 매핑 (verdict → 되돌림 가능성) ------------------------------------
# 비용 순위가 아니라 "되돌릴 수 있는가" 한 축이다(시작메모 §4-②). 이름 주의: 위험도 아님.
#   불가역 = 정비(챔버가 물리적으로 바뀜)          → equipment_fault
#   가역   = recipe ±3% · 관리선 0.5σ (되돌릴 수 있음) → process_shift · baseline_aging
#   무개입 = 이관(사람에게 넘김, 아무것도 안 바꿈)   → escalate
REVERSIBILITY: dict[str, str] = {
    "equipment_fault": "불가역",
    "process_shift": "가역",
    "baseline_aging": "가역",
    "escalate": "무개입",
}
REV_ORDER = ("불가역", "가역", "무개입")

# --- ③-C 변별 신호 탐지 (2026-07-24 재정의) -------------------------------------
# 구 정의는 rationale 에 `[KB` 문자열이 있으면 "매뉴얼-존재 근거"로 셌다 → **건강한 리포트를
#   병으로 셌다**. 출처 태그 병기는 Evidence Card 규칙상 필수라 정상 리포트에도 항상 있고,
#   실측(fewshot3)에서 LLM 은 오히려 "'항목 존재'는 정비 근거가 아니다 [KB]"라고 옳게 쓰면서도
#   25/25 로 걸렸다. 문구를 쫓는 대신 **행동과 근거의 불일치**를 잰다:
#       정비를 제안했는데(action≠null) 변별 신호(급변·anomaly)를 근거로 대지 못했나.
# 변별 신호 = 판정 트리 STEP 1-A 가 ①(정비)을 가르는 기준. "매뉴얼에 항목이 있다"는 대부분
#   센서에 존재하므로 변별력이 없다(라이브러리 §4).
_DISCRIMINATING = ("N1", "CRITICAL", "급변", "anomaly", "spike")
# 부정 문맥("급변 없음"·"anomaly 미동반")은 신호가 **있다**는 뜻이 아니다. rationale 은
#   "① … ② … ③ …" 절 나열이라 절 단위로 끊어 판정한다 (제안하면서 부재를 적은 모순도 포착).
_CLAUSE_SPLIT = re.compile(r"[①②③④⑤⑥]")
_NEGATIONS = ("없", "미동반", "부재", "아님", "불가", "약함", "낮음")


def _proposed_maintenance(report: dict[str, Any]) -> bool:
    """이 manual 리포트가 정비를 **제안**했나 (물러선 것과 구분).

    `action` 이 정본이지만 2026-07-24 이전 저장본에는 그 필드가 없다 → `escalate_reason`
    부재로 대체한다. 프롬프트 §3 규칙1 이 "제안 못 하면 action=null + escalate_reason 기재"를
    강제하므로 둘은 사실상 배타다 (구 라운드 소급 분석용 폴백 — 신규 라운드는 action 사용).
    """
    if "action" in report:
        return bool(report.get("action"))
    return not (report.get("escalate_reason") or "").strip()


def cites_discriminating(rationale: Optional[str]) -> bool:
    """급변(N1/CRITICAL)·anomaly 를 **있다**고 인용한 절이 하나라도 있나."""
    for clause in _CLAUSE_SPLIT.split(rationale or ""):
        if any(t in clause for t in _DISCRIMINATING) and not any(n in clause for n in _NEGATIONS):
            return True
    return False


def results_path(tag: Optional[str], path: Optional[str]) -> Path:
    """--path 우선, 없으면 --tag 로 supervisor_results_<tag>.jsonl 를 조립."""
    if path:
        p = Path(path)
        return p if p.is_absolute() else SERVICE_ROOT.parents[1] / p
    name = f"supervisor_results_{tag}.jsonl" if tag else "supervisor_results.jsonl"
    return SERVICE_ROOT / "notes" / "eval" / name


def load(path: Path) -> list[dict[str, Any]]:
    """jsonl 로드 — 깨진 줄은 스킵(6-2 정신)."""
    if not path.exists():
        raise SystemExit(f"결과 파일 없음: {path}")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _scored(records: list[dict]) -> list[dict]:
    """채점 대상 = 정답 있음 · 게이트 통과 · 판정 존재 (eval_supervisor.score 와 동일 정의)."""
    return [r for r in records
            if r.get("sup_verdict_answer") and not r.get("is_gated") and "sup_verdict_predict" in r]


def _conf_by_option(rec: dict) -> dict[str, float]:
    """tool_reports → {option_type: confidence}. 사후 분석의 핵심 원자재."""
    return {t["option_type"]: t.get("confidence", 0.0) for t in rec.get("tool_reports", [])}


# --- ③ 근거의 질 (맞은 건 = hit 기준) --------------------------------------------
def evidence_quality(scored: list[dict]) -> None:
    hits = [r for r in scored if r["sup_verdict_predict"] == r["sup_verdict_answer"]]
    n = len(hits)
    print("\n" + "=" * 72)
    print(f"③ 근거의 질 — '맞아도 제대로 맞았나' (정답/hit {n}건 기준)")
    print("=" * 72)
    if not n:
        print("  hit 0건 — 계산 대상 없음")
        return

    # A. 정답 옵션이 confidence 1위가 아니었나 (escalate 는 대응 옵션이 없어 제외)
    a_hits = [r for r in hits if VERDICT_TO_OPTION.get(r["sup_verdict_answer"]) is not None]
    a_not_top = []
    for r in a_hits:
        confs = _conf_by_option(r)
        ans_opt = VERDICT_TO_OPTION[r["sup_verdict_answer"]]
        if not confs or ans_opt not in confs:
            continue
        if confs[ans_opt] < max(confs.values()):  # 동점은 1위로 본다(관대)
            a_not_top.append(r)
    print(f"\n  A. 정답 옵션이 confidence 1위 아님 : {len(a_not_top)}/{len(a_hits)}  "
          f"← '맞혔지만 확신은 딴 데'")
    for r in a_not_top:
        confs = _conf_by_option(r)
        ans_opt = VERDICT_TO_OPTION[r["sup_verdict_answer"]]
        top = max(confs, key=confs.get)
        print(f"       {r['alert_id']}: 정답옵션 {ans_opt}={confs[ans_opt]:.2f} < "
              f"{top}={confs[top]:.2f}")

    # B. 카드 0장으로 맞힘 (근거 없이 정답 — 운·과적합 의심)
    b_zero = [r for r in hits if r.get("tool_cards_n", 0) == 0]
    print(f"\n  B. 근거카드 0장으로 맞힘        : {len(b_zero)}/{n}  ← 0 이어야 건강")
    for r in b_zero:
        print(f"       {r['alert_id']} ({r['sup_verdict_answer']})")

    # C. 변별 신호 없이 정비를 제안했나 (매뉴얼 '항목 존재'만으로 미는 패턴 — §4)
    #    분모 = 정비를 **제안한** manual 리포트. 물러선 리포트(action=null)는 대상이 아니다.
    proposed = [(r, t) for r in hits for t in r.get("tool_reports", [])
                if t.get("option_type") == "manual_option" and _proposed_maintenance(t)]
    c_hollow = [(r, t) for r, t in proposed if not cites_discriminating(t.get("rationale"))]
    denom = len(proposed)
    print(f"\n  C. 변별 신호 없이 정비 제안     : {len(c_hollow)}/{denom}  "
          f"← 0 이어야 건강 (분모=정비 제안한 manual)")
    for r, t in c_hollow:
        basis = f"action={t['action']!r}" if "action" in t else "action 미저장(escalate_reason 폴백)"
        print(f"       {r['alert_id']}: {basis} conf={t.get('confidence')}")
    print("     ※ 급변(N1/CRITICAL)·anomaly 를 '있다'고 인용했는지로 판정 — 절 단위 부정 문맥 제외.")


# --- ② 오답의 방향 (틀린 건 기준) ------------------------------------------------
def error_direction(scored: list[dict]) -> None:
    wrong = [r for r in scored if r["sup_verdict_predict"] != r["sup_verdict_answer"]]
    print("\n" + "=" * 72)
    print(f"② 오답의 방향 — '틀렸을 때 어디로' (오답 {len(wrong)}건 기준)")
    print("  🔷 멘토 확인 대기 축 — 계산은 근거 확보용, 프레임 확정은 멘토")
    print("=" * 72)
    if not wrong:
        print("  오답 0건")
        return

    # 가역성 혼동행렬 (행=정답의 가역성, 열=판정의 가역성)
    matrix: dict[str, Counter] = {k: Counter() for k in REV_ORDER}
    for r in wrong:
        a = REVERSIBILITY.get(r["sup_verdict_answer"])
        p = REVERSIBILITY.get(r["sup_verdict_predict"])
        if a and p:
            matrix[a][p] += 1
    w = 6
    print("\n  가역성 혼동행렬 (행=정답, 열=판정)")
    print("  " + " " * w + "".join(f"{c:>8}" for c in REV_ORDER))
    for a in REV_ORDER:
        row = "".join(f"{matrix[a][p] or '·':>8}" for p in REV_ORDER)
        print(f"  {a:<{w}}{row}")

    # 헤드라인: 불가역 오답 = 되돌릴 수 있는(또는 무개입) 사안을 불가역(정비)으로 오판
    irrev = [r for r in wrong
             if REVERSIBILITY.get(r["sup_verdict_predict"]) == "불가역"
             and REVERSIBILITY.get(r["sup_verdict_answer"]) != "불가역"]
    print(f"\n  🔴 불가역 오답 : {len(irrev)}/{len(wrong)}  "
          f"← 되돌릴 수 있는/이관할 사안을 '정비'로 오판 (제일 비싼 실수)")
    for r in irrev:
        print(f"       {r['alert_id']}: 정답={r['sup_verdict_answer']}"
              f"({REVERSIBILITY.get(r['sup_verdict_answer'])}) → "
              f"판정={r['sup_verdict_predict']}(불가역)")

    # 반대 방향: 무개입(이관)해야 하는데 개입 (놓친 에스컬레이션)
    missed_esc = [r for r in wrong
                  if REVERSIBILITY.get(r["sup_verdict_answer"]) == "무개입"
                  and REVERSIBILITY.get(r["sup_verdict_predict"]) != "무개입"]
    print(f"  🟡 무개입 놓침 : {len(missed_esc)}/{len(wrong)}  "
          f"← 이관했어야 하는데 스스로 조치 판정")


# --- ④ 판단인가 복제인가 (2026-08-06 신설 — W55) ----------------------------------
# 🔴 **왜 필요한가.** round18 에서 LLM 이 프롬프트 예시를 통째로 베끼고 있었다 —
#    42건 중 20건(48%)이 같은 근거 문장, 오답 7건은 7/7 전부. 그런데 그걸 **일회용 스크립트로
#    손으로** 잡았다. 도구가 아니라 습관에 의존하면 다음엔 못 잡는다.
#
#    그리고 "옛 예시 문장이 안 나온다"는 증명이 못 된다 — 프롬프트에서 뺐으니 당연하다.
#    봐야 할 것은 **베끼는 대상이 무엇이든 근거가 서로 얼마나 같은가** 다.
#    서로 다른 사안 42건인데 근거가 몇 종류인가 — 그게 판단과 템플릿을 가른다.
_PLACEHOLDER = re.compile(r"<[^>]{2,}>")


def _norm(line: str) -> str:
    """수치·ID 를 지운 뼈대만 남긴다 — 값만 바꿔 끼운 같은 문장을 같은 것으로 센다."""
    s = re.sub(r"\[[^\]]*\]", "[]", line)          # 출처 태그·인용 ID
    s = re.sub(r"[0-9]+(?:\.[0-9]+)?", "#", s)     # 모든 수치
    return re.sub(r"\s+", " ", s).strip()


def judgment_or_copy(records: list[dict]) -> None:
    """근거 문장의 **다양성**과 플레이스홀더 누출을 잰다."""
    live = [r for r in records if not r.get("is_gated")]
    lines = [e for r in live for e in (r.get("sup_brief") or {}).get("evidence", []) if e]
    print("\n" + "=" * 72)
    print(f"④ 판단인가 복제인가 — 근거 문장의 다양성 (가동 {len(live)}건)")
    print("=" * 72)
    if not lines:
        print("  근거 0줄 — 계산 대상 없음")
        return

    shapes = Counter(_norm(x) for x in lines)
    uniq, top_n, top_txt = len(shapes), *shapes.most_common(1)[0][::-1]
    dup = sum(n for _, n in shapes.items() if n > 1)
    print(f"  근거 총 {len(lines)}줄 · 서로 다른 뼈대 {uniq}종  (다양성 {uniq / len(lines) * 100:.0f}%)")
    print(f"  2건 이상 반복된 줄 : {dup}/{len(lines)}  ({dup / len(lines) * 100:.0f}%)")
    print(f"  최다 반복          : {top_n}회  \"{top_txt[:64]}\"")
    print("     ※ 다양성이 낮을수록 사안이 아니라 **틀**을 쓰고 있다는 뜻이다.")
    print("       (실측 기준선: round18=예시 복사 상태 · round19=예시 제거 직후)")

    # 플레이스홀더 누출 — 중립화한 예시의 <...> 자리가 출력에 그대로 나오면 그것도 복제다
    leaked = [x for x in lines if _PLACEHOLDER.search(x)]
    mark = "✅" if not leaked else "🔴"
    print(f"\n  {mark} 예시 자리표시(<...>) 누출 : {len(leaked)}줄  ← 0 이어야 한다")
    for x in leaked[:3]:
        print(f"       {x[:70]}")


# --- ③-D 옵션별 확신 분포 (2026-08-06 신설) ---------------------------------------
def confidence_spread(records: list[dict]) -> None:
    """옵션별 confidence 분포 — **예시가 값을 가르치면 여기 최빈값으로 드러난다.**

    실측(round18): limit 0.85 가 29/42 · recipe 0.4 가 18/42 — 전부 few-shot 예시의 값이었고,
    `limit 0.85 > maint 0.79 > recipe 0.78` 이라는 예시 서열이 그대로 판정 서열이 됐다.
    """
    live = [r for r in records if not r.get("is_gated")]
    by: dict[str, list[float]] = {}
    for r in live:
        for opt, c in _conf_by_option(r).items():
            if c is not None:
                by.setdefault(opt, []).append(round(float(c), 2))
    print("\n" + "=" * 72)
    print("③-D 옵션별 확신 분포 — 예시 값 고착 탐지")
    print("=" * 72)
    for opt in sorted(by):
        v = by[opt]
        c = Counter(v)
        top, n = c.most_common(1)[0]
        flag = "🔴 고착" if n / len(v) >= 0.5 else ("🟡" if n / len(v) >= 0.3 else "✅")
        print(f"  {opt:<16} n={len(v):<4} 평균 {sum(v)/len(v):.2f}  "
              f"최빈 {top} ({n}/{len(v)} = {n/len(v)*100:.0f}%)  {flag}")
    print("     ※ 한 값이 절반을 넘으면 근거에서 산출한 것이 아니라 **어딘가에서 복사**한 것이다.")


#: D4 지연 예산 폴백 — `agent.slow_path_max_sec` 를 못 읽을 때만 쓴다.
_D4_BUDGET_DEFAULT = 30


def _d4_budget_sec() -> float:
    """D4 slow-path 지연 예산(초). 정본은 `config/params.yaml` `agent.slow_path_max_sec`.

    판정하는 쪽(`app/pipeline.py`)과 **같은 값을 봐야** 한다 — 여기만 상수로 두면
    예산이 바뀌었을 때 분석 스크립트만 낡은 기준으로 "초과 N건"을 센다
    (config→소비처 표류. 런타임은 안 깨지고 숫자만 조용히 틀린다 — 헌법 7장).
    """
    try:
        from .app.config import load_settings
        return float(load_settings().require("agent.slow_path_max_sec"))
    except Exception:      # noqa: BLE001 — 분석 도구는 config 없이도 돌아야 한다
        return float(_D4_BUDGET_DEFAULT)


# --- 라운드 요약 (2026-08-06 신설 — W66) --------------------------------------------
def round_metrics(records: list[dict]) -> dict[str, Any]:
    """②③④·지연·토큰을 **한 줄로 압축**해 돌려준다 — `runs_history` 에 실릴 형.

    🔴 **왜 필요한가.** 그동안 `runs_history` 에는 ①(정확도·판정별)만 남았다. 그래서
    *"②③④·지연이 라운드마다 어떻게 변했나"* 를 볼 때마다 **일회용 스크립트로 다시 계산**했다
    (2026-08-06 하루에만 여러 번). 도구가 아니라 습관에 의존하면 다음엔 안 한다.
    그리고 손으로 만든 추이표는 **재현되지 않는다** — 지금 그 표를 다시 그리려면 또 짜야 한다.

    ⚠️ 위의 출력 함수들과 **같은 정의를 두 번 쓴다.** 갈라지지 않게 테스트가 둘을 대조한다
    (`test_round_metrics_matches_printed_report`).
    """
    live = [r for r in records if not r.get("is_gated")]
    scored = _scored(records)
    out: dict[str, Any] = {}

    # ② 오답의 방향 — 불가역 오판이 제일 비싼 실수다
    wrong = [r for r in scored if r["sup_verdict_predict"] != r["sup_verdict_answer"]]
    out["err_n"] = len(wrong)
    out["err_irreversible"] = sum(
        1 for r in wrong
        if REVERSIBILITY.get(r["sup_verdict_answer"]) != "불가역"
        and REVERSIBILITY.get(r["sup_verdict_predict"]) == "불가역")
    out["err_missed_escalation"] = sum(
        1 for r in wrong
        if REVERSIBILITY.get(r["sup_verdict_answer"]) == "무개입"
        and REVERSIBILITY.get(r["sup_verdict_predict"]) != "무개입")

    # ③ 근거의 질
    hits = [r for r in scored if r["sup_verdict_predict"] == r["sup_verdict_answer"]]
    not_top = 0
    for r in hits:
        ans_opt = VERDICT_TO_OPTION.get(r["sup_verdict_answer"])
        confs = _conf_by_option(r)
        if ans_opt and ans_opt in confs and confs[ans_opt] < max(confs.values()):
            not_top += 1
    out["hit_n"] = len(hits)
    out["hit_conf_not_top"] = not_top
    out["hit_zero_cards"] = sum(1 for r in hits if not (r.get("tool_cards_n") or 0))

    # ③-D 옵션별 확신 고착 — 한 값이 절반을 넘으면 산출이 아니라 복사다
    for opt in ("recipe_option", "limit_option", "manual_option"):
        v = [c for r in live for o, c in _conf_by_option(r).items() if o == opt and c is not None]
        if v:
            top, n = Counter(round(float(x), 2) for x in v).most_common(1)[0]
            out[f"conf_mode_{opt.split('_')[0]}"] = top
            out[f"conf_stick_{opt.split('_')[0]}_pct"] = round(n / len(v) * 100, 1)

    # ④ 판단인가 복제인가
    lines = [e for r in live for e in (r.get("sup_brief") or {}).get("evidence", []) if e]
    if lines:
        shapes = Counter(_norm(x) for x in lines)
        out["evi_lines"] = len(lines)
        out["evi_diversity_pct"] = round(len(shapes) / len(lines) * 100, 1)
        out["evi_max_repeat"] = shapes.most_common(1)[0][1]
        out["evi_placeholder_leak"] = sum(1 for x in lines if _PLACEHOLDER.search(x))

    # 인용 가드
    out["masked_citations"] = sum(r.get("sup_masked_citations") or 0 for r in live)

    # 지연·토큰 — D4 예산 판정은 결과 파일만으로 되어야 한다
    lat = sorted(r["obs_latency_total_sec"] for r in live if r.get("obs_latency_total_sec"))
    if lat:
        out["lat_median_sec"] = lat[len(lat) // 2]
        out["lat_max_sec"] = lat[-1]
        out["lat_over_d4_n"] = sum(1 for x in lat if x > _d4_budget_sec())
        out["lat_over_d4_pct"] = round(out["lat_over_d4_n"] / len(lat) * 100, 1)
    tok = sorted(r["obs_prompt_tokens"] for r in live if r.get("obs_prompt_tokens"))
    if tok:
        out["prompt_tokens_median"] = tok[len(tok) // 2]
    ret = [r.get("obs_retrieved_n") or 0 for r in live]
    if ret:
        out["retrieved_per_alert"] = round(sum(ret) / len(ret), 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="근거의 질(③)·오답 방향(②)·복제 탐지(④) 사후 분석기")
    ap.add_argument("--tag", type=str, help="라운드 라벨 (supervisor_results_<tag>.jsonl)")
    ap.add_argument("--path", type=str, help="결과 jsonl 직접 지정 (--tag 보다 우선)")
    args = ap.parse_args()

    path = results_path(args.tag, args.path)
    records = load(path)
    scored = _scored(records)
    hits = sum(1 for r in scored if r["sup_verdict_predict"] == r["sup_verdict_answer"])
    print(f"\n분석 대상: {path.name}")
    print(f"전체 {len(records)}건 · 채점 {len(scored)}건 · 정답 {hits}건 "
          f"({hits / len(scored) * 100:.1f}%)" if scored else "채점 대상 0건")

    evidence_quality(scored)
    confidence_spread(records)
    error_direction(scored)
    judgment_or_copy(records)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
