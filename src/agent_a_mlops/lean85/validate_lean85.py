# -*- coding: utf-8 -*-
"""validate_lean85.py — CT①(lean85) challenger 자동 검증 게이트 (WP-3 / 설계 D4·D12·**D13**).

설계 정본: `CT1_배선_설계_v2.md` (v1 대체 — D13 절대 열화 가드가 v2 신설 확정).

**이 모듈이 게이트 항목·임계의 단일 소스다** — 헌법·설계서는 수치를 규정하지 않는다
(설계 D12: CT² D12 선례 "테이블 개수는 init.sql 이 단일 소스"·"매직 넘버는 전부
params.yaml" 의 연장). 임계 기본값은 아래 `DEFAULTS` 이고 `config/params.yaml`
`ct.ct1_*` 로 덮어쓴다. 항목 **추가·임계 강화는 여기/config 변경만으로** 가능하되,
**항목 삭제·완화는 PM 승인 + PR 사유 명시**가 필요하다 (헌법 3-3 ③ ⓑ 준용).

검증 4군 (설계 §4-4):
  A. 완결성   산출물 존재 · 네이티브 로드 · 폴더명 ≤32자 · 피처 계약(85·R2·R10)
  B. 누수·창  학습셋 최대 시각 ≤ 컷오프(raise) · 학습∩평가 wafer = 0(raise) ·
              완결 요란 레짐 조항 이행(window_meta 대조)
  C. 표본     학습 wafer 하한 · 평가 wafer 하한 — **미달은 PASS 가 아니라 SKIP**(판정 불가)
  D. 승격판정 D1 C–C: 공통 평가셋(라벨 완결 + **온셋 제외**)의 pooled RMSE 개선 ≥ 임계
              D2 절대 상한(D13, v2): challenger pooled RMSE ≤ BENCH_POOLED × 배수 —
                 **상대 개선율과 무관**. 초과 시 SKIP (champion 유지)
              ※ champion `rmse_before` 도 같은 상한과 대조해 `metrics.champion_over_cap`
                 으로 기재만 한다 — **연속 초과 판정·에스컬레이션은 오케스트레이터**다.

왜 D2 가 필요한가: D1 은 상대 비교라 champion·challenger 가 **같은 오염**을 학습하면
무력하다(gain<임계 → SKIP → 결함 champion 이 그대로 앉는다). AE 라벨 오염 38장이 상대
지표·게이트·PM 보고를 전부 통과한 실증이 헌법 7장에 있다. D2 가 잡는 것은 "붕괴"이지
"왜곡"이 아니다 — 상한 안쪽의 미세 오염은 여전히 불가시다 (설계 §10-8, 정직한 한계).

왜 온셋을 평가에서 빼는가: 요란 PM 직후 ONSET_DAYS 구간은 어떤 모델도 오차가 급증한다
(B2′ 실측 fold 147/113/109). 그 구간이 섞이면 두 모델의 차이가 공통 잡음에 묻혀
champion–challenger 판정이 무뎌진다. 참고로 **온셋 포함 pooled 도 리포트에 병기**한다
(판정에는 쓰지 않는다 — 실증 결과서의 "두 수치 병기 의무"와 같은 취지).

공정성 전제 (설계 D4·§10-7): champion 도 자기 승격 시점의 `d′ − H` 까지만 학습했어야
평가창이 양쪽 모두 미학습이 된다. 규율 이전에 만들어진 초기 champion 은 이 전제가
성립하지 않으므로 B4 항목이 **경고**로 남긴다 (판정에는 쓰지 않는다 — 첫 비교를 통째로
막으면 개통 자체가 불가능하다).

exit code:
    0 = PASS(승격 가능) · 2 = FAIL(게이트 차단) · 3 = SKIP(판정 불가·미달) · 1 = 실행 오류
    ※ CT²(`ae_pipeline/validate_bundle`)의 0/2/1 규약에 **3(SKIP)을 추가**한 것이다.
      CT² 에는 champion 비교가 없어 SKIP 이라는 결과 자체가 없다. 판정 불가를 PASS 로
      오해하지 않는다는 원칙은 동일하다.

CLI:
    python validate_lean85.py --challenger <stamp_dir> --champion <stamp_dir|auto> \
        --eval-data <ct1_evalset_*.csv> --window-meta <ct1_window_*.json> \
        --out <challenger>/validation.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lean85_pipeline as lp  # noqa: E402  (피처·모델·창 산식 단일 소스)

log = logging.getLogger("ct1.validate")

REPO_ROOT = lp.REPO_ROOT
VALIDATION_REPORT = "validation.json"          # = ct1_orchestrator.VALIDATION_REPORT_NAME
EXIT_PASS, EXIT_ERROR, EXIT_FAIL, EXIT_SKIP = 0, 1, 2, 3

# ── 게이트 임계 기본값 (params.yaml `ct.ct1_*` 로 override — 헌법 6-1) ─────────
# ⚠️ 출발값이다. 승격 임계 3.0%는 실증(이음새 제외 +3.87%)과 근접하므로 승격 빈도·
#    flapping 은 WP-8 리허설의 H·임계 스윕에서 확인해야 한다 (설계 §10-1).
DEFAULTS = {
    "promote_min_rmse_gain_pct": 3.0,   # D: pooled RMSE 개선 하한(%) — 승격 판정
    "min_train_wafers": 1000,           # C: 학습 표본 하한 (실험 하네스 하한 100의 운영 상향)
    "min_eval_wafers": 100,             # C: 평가 표본 하한 — 미달 시 SKIP(판정 불가)
    "stamp_name_max": lp.STAMP_NAME_MAX,  # A: 감사 컬럼 폭(VARCHAR(32))
    "features_n": 85,                   # A: lean-85 계약
    # D13 (설계 v2 §4-7): 절대 상한 = BENCH_POOLED(99.84) × ratio. 일간 실측 pooled
    # 54.80 과 무재학습 발산 334.24 사이의 판별 앵커다. 배수는 잠정치 — WP-8 스윕 확정.
    "abs_rmse_max_ratio": 1.5,
    # D14 (설계 v2 §4-8 ⓒ): 리로드 스모크 절대범위. **config 키를 늘리지 않고 여기 둔다**
    # (설계 §6 — 키 남발 방지). 학습 타깃 C65 실측 지지구간
    # (synthetic_1y 80,294 wafer: min 481.2 · p0.1 526.6 · p99.9 1592.0 · max 1678.0)에
    # 넉넉한 여유를 둔 값이다. **정확도 판정이 아니라 동작성 판정**이므로 느슨한 게 맞다 —
    # 여기 걸리는 건 0·음수·폭주 같은 병리적 출력이지 "조금 빗나간 예측"이 아니다.
    "smoke_pred_min": 100.0,
    "smoke_pred_max": 5000.0,
}
# params.yaml 키 ↔ DEFAULTS 키 (config 는 `ct1_` 접두 + `gate_` 군을 쓴다)
_PARAM_KEYS = {
    "ct1_promote_min_rmse_gain_pct": "promote_min_rmse_gain_pct",
    "ct1_gate_min_train_wafers": "min_train_wafers",
    "ct1_gate_min_eval_wafers": "min_eval_wafers",
    "ct1_gate_abs_rmse_max_ratio": "abs_rmse_max_ratio",       # D13 (설계 v2)
}


def load_gate_params(params_path=None) -> dict:
    """`ct.ct1_*` 로 DEFAULTS override. 로드 실패 시 DEFAULTS 유지(+경고)."""
    gate = dict(DEFAULTS)
    try:
        import yaml
        p = Path(params_path) if params_path else REPO_ROOT / "config" / "params.yaml"
        with open(p, encoding="utf-8") as f:
            ct = (yaml.safe_load(f) or {}).get("ct") or {}
        for src, dst in _PARAM_KEYS.items():
            if src in ct:
                gate[dst] = ct[src]
    except Exception as e:                                  # noqa: BLE001
        log.warning("게이트 임계 로드 실패 → DEFAULTS 사용: %s", e)
    return gate


def _c(name: str, ok: bool, detail: str, critical: bool = False) -> dict:
    """검사 1건 결과 dict (CT² 리포트 규약과 동일 키 — 계약 §8-E)."""
    return {"check": name, "pass": bool(ok), "detail": detail, "critical": bool(critical)}


def _rmse(y, p) -> float:
    """RMSE — 비유한값이 섞이면 NaN 이 아니라 예외로 드러낸다."""
    y, p = np.asarray(y, float), np.asarray(p, float)
    if not (np.isfinite(y).all() and np.isfinite(p).all()):
        raise ValueError("평가셋에 비유한값(NaN/Inf)이 있습니다 — 조립 경로 점검 필요")
    return float(np.sqrt(np.mean((y - p) ** 2)))


def build_eval_table(eval_csv, pm_log, lean) -> pd.DataFrame:
    """평가 raw → 웨이퍼 테이블 (+ `low_confidence`). 라벨 없는 wafer 는 제외.

    온셋 판정은 `lp.low_confidence_flags` **단일 소스**를 쓴다 — 조립기가 같은 식을
    재구현하지 않는 이유(설계 §4-3).
    """
    raw = pd.read_csv(eval_csv)
    tbl = lp.build_wafer_table(raw, pm_log, lean)
    if lp.TARGET_COL not in tbl.columns:
        raise ValueError(f"평가셋에 타깃 {lp.TARGET_COL} 없음 — champion–challenger 판정 불가")
    tbl = tbl[tbl[lp.TARGET_COL].notna()].reset_index(drop=True)
    tbl["low_confidence"] = lp.low_confidence_flags(tbl).to_numpy()
    return tbl


def _model_glob() -> str:
    """`lean85.model_search_glob` (기본 타입-우선 구조 — 헌법 4-1·6-4)."""
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            g = ((yaml.safe_load(f) or {}).get("lean85") or {}).get("model_search_glob")
        if g:
            return g
    except Exception as e:                                   # noqa: BLE001
        log.warning("model_search_glob 로드 실패 → 기본 glob: %s", e)
    return "models/c65_predictor/*/lean85_*"


def resolve_champion(spec: str | None, *, exclude: Path | None = None) -> Path | None:
    """champion stamp 폴더 해석 — `none` = 비교 대상 없음, `auto` = 포인터 → glob 폴백.

    ⚠️ **`exclude`(= challenger)를 반드시 후보에서 뺀다.** stamp 는 사전순 = 시간순이라
    방금 만든 challenger 가 glob 최신이 되고, 그러면 "champion 부재 = 무조건 승격"
    분기로 빠져 champion–challenger 비교가 통째로 생략된다 — 헌법 3-3 ① 우회로다
    (리뷰 B2).

    Args:
        spec: `"none"`(비교 없음 명시) · `"auto"`/None(자동 해석) · 그 외는 경로.
        exclude: 후보에서 뺄 경로 (challenger 자기 자신).

    Returns:
        champion 폴더 Path, 또는 None(진짜로 비교 대상이 없을 때).
    """
    ex = Path(exclude).resolve() if exclude else None
    if spec and spec not in ("auto", "none"):
        p = Path(spec)
        return None if (ex and p.resolve() == ex) else p
    if spec == "none":
        return None
    ptr = REPO_ROOT / "control" / "ct1" / "champion.json"
    if ptr.exists():
        try:
            d = json.loads(ptr.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("model_dir"):   # json.loads 성공 ≠ dict (헌법 7장)
                p = Path(d["model_dir"])
                if not (ex and p.resolve() == ex):
                    return p
            else:
                log.warning("champion 포인터가 객체/경로 아님 → glob 폴백: %s", ptr)
        except Exception as e:                               # noqa: BLE001
            log.warning("champion 포인터 판독 실패 → glob 폴백: %s", e)
    hits = [p for p in sorted(REPO_ROOT.glob(_model_glob()))
            if p.is_dir() and not (ex and p.resolve() == ex)]
    return hits[-1] if hits else None


def other_models_exist(exclude: Path) -> bool:
    """challenger 외에 다른 lean85 stamp 폴더가 존재하는가 (부트스트랩 판별용).

    "champion 부재"가 **정말 최초 학습**인지, 아니면 해석 실패인지를 가른다. 실물 모델이
    디스크에 있는데 champion 을 못 찾았다면 그것은 부트스트랩이 아니라 **사고**이므로
    무조건 승격시키면 안 된다 (리뷰 B2).
    """
    ex = Path(exclude).resolve()
    return any(p.is_dir() and p.resolve() != ex for p in REPO_ROOT.glob(_model_glob()))


def _pooled(model, tbl: pd.DataFrame, lean, mask=None) -> float:
    """pooled RMSE (mask 지정 시 해당 부분집합)."""
    t = tbl if mask is None else tbl[mask]
    return _rmse(t[lp.TARGET_COL].to_numpy(float), model.predict(t[lean]))


def validate(challenger_dir, eval_csv, *, champion_dir=None, window_meta=None,
             pm_log=None, params_path=None) -> dict:
    """challenger + 평가셋 → 게이트 리포트 dict (`gate_verdict` 가 최종 판정).

    Args:
        challenger_dir: 재학습 산출 stamp 폴더.
        eval_csv: `ct1_assemble_trainset` 이 낸 평가셋 raw CSV.
        champion_dir: 비교 대상. None/`"auto"` = 운영 포인터 → glob 폴백.
        window_meta: 조립기가 낸 창 메타 dict (B군 대조 소스). None 이면 challenger
            manifest 의 `ct1_window_meta` 를 쓴다.
        pm_log: 평가 테이블 피처용 pm_log (기본 자동 탐색).
        params_path: params.yaml override.

    Returns:
        리포트 dict — `gate_verdict` ∈ {PASS, FAIL, SKIP}, `gate_pass` 는 PASS 여부.
    """
    gate = load_gate_params(params_path)
    challenger = Path(challenger_dir)
    checks: list[dict] = []
    metrics: dict = {}
    skip_reasons: list[str] = []

    lean, _, _ = lp.load_frozen()
    pm_p = pm_log or lp.find_file("pm_log.json")

    # ── A군: 완결성 ──────────────────────────────────────────────────────
    need = ["lean85_model.json", "manifest.json"]
    absent = [f for f in need if not (challenger / f).exists()]
    checks.append(_c("A1_산출물_존재", not absent,
                     "lean85_model.json·manifest.json 실존" if not absent
                     else f"누락: {absent}", critical=True))

    checks.append(_c("A3_폴더명_길이", len(challenger.name) <= gate["stamp_name_max"],
                     f"{len(challenger.name)}자 ≤ {gate['stamp_name_max']} ('{challenger.name}')"
                     if len(challenger.name) <= gate["stamp_name_max"]
                     else f"{len(challenger.name)}자 초과 — 감사 컬럼 VARCHAR(32) 절단 위험",
                     critical=True))

    chal_model = chal_mani = None
    if not absent:
        try:
            chal_model, chal_mani = lp.load_model(challenger)
            checks.append(_c("A2_네이티브_로드", True,
                             f"xgb.load_model OK (학습 {chal_mani.get('created_at_local', '?')[:19]})"))
        except Exception as e:                               # noqa: BLE001
            checks.append(_c("A2_네이티브_로드", False, f"로드 실패: {e}", critical=True))
    else:
        checks.append(_c("A2_네이티브_로드", False, "산출물 부재로 로드 불가", critical=True))

    try:
        lp.check_feature_contract(lean)
        # 개수만 보면 **다른 85피처**로 학습된 모델이 통과한다 — 동결 목록의 sha1 까지
        # 대조해야 "lean-85" 라는 이름이 실제로 같은 계약을 가리킨다 (리뷰 S7).
        want_sha = hashlib.sha1(",".join(lean).encode()).hexdigest()[:12]
        got_n = chal_mani.get("features_n") if chal_mani else None
        got_sha = chal_mani.get("features_sha1") if chal_mani else None
        ok = bool(chal_mani) and got_n == gate["features_n"] and got_sha == want_sha
        checks.append(_c("A4_피처_계약", ok,
                         f"lean-85 계약 OK · features_n={got_n} · sha1={got_sha}" if ok else
                         f"불일치: features_n={got_n}(기대 {gate['features_n']}) · "
                         f"sha1={got_sha}(기대 {want_sha})", critical=True))
    except Exception as e:                                   # noqa: BLE001
        checks.append(_c("A4_피처_계약", False, f"피처 계약 위반: {e}", critical=True))

    # ── B군: 누수·창 ─────────────────────────────────────────────────────
    wmeta = window_meta or ((chal_mani or {}).get("ct1_window_meta") or {})
    metrics["window_meta"] = wmeta or None

    eval_tbl = None
    try:
        eval_tbl = build_eval_table(eval_csv, pm_p, lean)
    except Exception as e:                                   # noqa: BLE001
        checks.append(_c("B0_평가셋_판독", False, f"평가셋 판독 실패: {e}", critical=True))

    if wmeta and chal_mani:
        try:
            # 학습 상한은 manifest 의 실제 학습 기간 최댓값으로 판정한다 — 조립기가
            # 낸 창 메타를 믿는 게 아니라 **모델이 실제로 무엇을 봤는지**를 본다.
            lp.assert_window_no_leakage([chal_mani["train_period"][1]], wmeta["cutoff"])
            checks.append(_c("B1_학습_컷오프", True,
                             f"학습 최대 {chal_mani['train_period'][1][:19]} ≤ 컷오프 "
                             f"{wmeta['cutoff'][:19]}", critical=True))
        except Exception as e:                               # noqa: BLE001
            checks.append(_c("B1_학습_컷오프", False, str(e), critical=True))
    else:
        checks.append(_c("B1_학습_컷오프", False,
                         "창 메타 또는 manifest 부재 — 누수 검증 불가 (판정을 PASS 로 "
                         "환원하지 않는다)", critical=True))

    if eval_tbl is not None and wmeta:
        try:
            ets = pd.to_datetime(eval_tbl["wf_ts"])
            cutoff = pd.Timestamp(wmeta["cutoff"])
            bad = int((ets <= cutoff).sum())
            if bad:
                raise ValueError(
                    f"헌법 1-3 위반: 평가셋에 컷오프({cutoff}) 이전 wafer {bad}장 "
                    "— challenger 가 학습한 구간이 평가에 섞였다")
            checks.append(_c("B2_평가창_분리", True,
                             f"평가 wafer 전량 컷오프 이후 ({str(ets.min())[:19]} ~ "
                             f"{str(ets.max())[:19]})", critical=True))
        except Exception as e:                               # noqa: BLE001
            checks.append(_c("B2_평가창_분리", False, str(e), critical=True))
    else:
        checks.append(_c("B2_평가창_분리", False, "평가셋/창 메타 부재로 검증 불가",
                         critical=True))

    # B2b — 학습∩평가 wafer = 0 을 **게이트가 독립 수행**한다. 조립기 안의 같은 검사는
    # 마스크에서 파생된 집합끼리 비교라 항진명제에 가깝다(구조상 겹칠 수 없다). 여기서는
    # 실제로 학습에 들어간 CSV 의 C64 를 읽어 평가셋과 대조하므로, 조립 경로가 바뀌어도
    # 헌법 1-3(그룹 분할)이 코드로 강제된다 (리뷰 S7).
    trainset = (wmeta or {}).get("trainset")
    if eval_tbl is not None and trainset and Path(trainset).exists():
        try:
            tr_ids = set(pd.read_csv(trainset, usecols=[lp.ID_COL])[lp.ID_COL].unique())
            lp.assert_disjoint_wafers(tr_ids, set(eval_tbl[lp.ID_COL]))
            checks.append(_c("B2b_그룹_분할", True,
                             f"학습 {len(tr_ids):,} wafer ∩ 평가 {len(eval_tbl):,} wafer = 0",
                             critical=True))
        except Exception as e:                               # noqa: BLE001
            checks.append(_c("B2b_그룹_분할", False, str(e), critical=True))
    else:
        # 학습셋을 확인할 수 없으면 '통과'가 아니라 **검사 미수행**임을 항목으로 남긴다
        # (C 군이 학습 오염을 조용히 통과시킨 CT² 의 실패를 반복하지 않는다).
        checks.append(_c("B2b_그룹_분할", False,
                         f"학습셋 경로 확인 불가(window_meta.trainset={trainset}) — "
                         "교집합 검사 미수행. 판정을 PASS 로 환원하지 않는다", critical=True))

    if wmeta:
        req = bool(wmeta.get("require_complete_loud_regime"))
        n_reg = int(wmeta.get("complete_loud_regimes_in_window") or 0)
        # ⚠️ 구 판정식(`n_reg>=1 or extended or extend_failed`)은 **절대 거짓이 될 수
        # 없었다** — slice_train_window 가 <1 일 때 반드시 둘 중 하나를 세우기 때문이다
        # (리뷰 S8). 여기서는 조항이 *실제로 이행됐는지*를 본다: 확장했다면 창 시작이
        # 그 레짐 시작과 일치하고 실효창이 명목창 이상이어야 하며, 미확장이면 창 안에
        # 완결 레짐이 실재해야 한다.
        problems = []
        eff, nom = (wmeta.get("effective_window_days") or 0), (wmeta.get("window_days") or 0)
        if wmeta.get("extended"):
            used = wmeta.get("regime_used")
            if not used:
                problems.append("확장했다는데 regime_used 없음")
            elif pd.Timestamp(wmeta["start"]) != pd.Timestamp(used[0]):
                problems.append(f"창 시작 {wmeta['start']} ≠ 레짐 시작 {used[0]}")
            if eff < nom:
                problems.append(f"확장했는데 실효창 {eff} < 명목 {nom}")
        elif req and n_reg < 1 and not wmeta.get("extend_failed"):
            problems.append("완결 레짐 0개인데 확장도 워밍업 표시도 없음")
        elif not wmeta.get("extend_failed") and abs(eff - nom) > 1e-6:
            problems.append(f"미확장인데 실효창 {eff} ≠ 명목 {nom}")
        detail = (f"완결 요란 레짐 {n_reg}개 · 확장={wmeta.get('extended')} "
                  f"(+{wmeta.get('extended_days', 0)}일) · 실효창 {eff}일/명목 {nom}일")
        if wmeta.get("extend_failed"):
            detail += " · ⚠워밍업(완결 레짐 부재 — 전환 표본 희소)"
        checks.append(_c("B3_요란레짐_조항", not problems,
                         detail if not problems else f"{detail} — 위반: {problems}",
                         critical=True))
    else:
        checks.append(_c("B3_요란레짐_조항", False, "창 메타 부재", critical=True))

    # ── C군: 표본 (미달 = SKIP — PASS 도 FAIL 도 아니다) ──────────────────
    n_train = int((chal_mani or {}).get("n_wafers_train") or 0)
    metrics["n_train_wafers"] = n_train
    ok_tr = n_train >= int(gate["min_train_wafers"])
    checks.append(_c("C1_학습_표본", ok_tr,
                     f"학습 {n_train:,}장 (하한 {int(gate['min_train_wafers']):,})"))
    if not ok_tr:
        skip_reasons.append(f"학습 표본 {n_train} < {int(gate['min_train_wafers'])}")

    n_eval = len(eval_tbl) if eval_tbl is not None else 0
    n_eval_scored = int((eval_tbl["low_confidence"] == 0).sum()) if eval_tbl is not None else 0
    metrics.update({"n_eval_wafers": n_eval, "n_eval_scored": n_eval_scored,
                    "n_eval_onset_excluded": n_eval - n_eval_scored})
    ok_ev = n_eval_scored >= int(gate["min_eval_wafers"])
    checks.append(_c("C2_평가_표본", ok_ev,
                     f"평가 {n_eval_scored:,}장(온셋 {n_eval - n_eval_scored:,}장 제외) "
                     f"(하한 {int(gate['min_eval_wafers']):,})"))
    if not ok_ev:
        skip_reasons.append(f"평가 표본 {n_eval_scored} < {int(gate['min_eval_wafers'])}")

    # ── D군: champion–challenger ────────────────────────────────────────
    champ_path = resolve_champion(champion_dir, exclude=challenger)
    champ_model = champ_mani = None
    if champ_path:
        try:
            champ_model, champ_mani = lp.load_model(champ_path)
        except Exception as e:                           # noqa: BLE001
            log.warning("champion 로드 실패 — 비교 생략: %s", e)
            champ_path = None
    metrics["champion"] = str(champ_path) if champ_path else None
    metrics["champion_name"] = Path(champ_path).name if champ_path else None

    gain = None
    if eval_tbl is not None and chal_model is not None and ok_ev:
        scored = (eval_tbl["low_confidence"] == 0).to_numpy()
        try:
            metrics["rmse_after"] = round(_pooled(chal_model, eval_tbl, lean, scored), 4)
            metrics["rmse_after_incl_onset"] = round(_pooled(chal_model, eval_tbl, lean), 4)
        except Exception as e:                           # noqa: BLE001
            checks.append(_c("D1_champion_challenger", False,
                             f"challenger 채점 실패: {e}", critical=True))
            champ_model = None
    if champ_model is not None and eval_tbl is not None and chal_model is not None and ok_ev:
        scored = (eval_tbl["low_confidence"] == 0).to_numpy()
        metrics["rmse_before"] = round(_pooled(champ_model, eval_tbl, lean, scored), 4)
        metrics["rmse_before_incl_onset"] = round(_pooled(champ_model, eval_tbl, lean), 4)
        before = metrics["rmse_before"]
        if before <= 0:                                  # 분모 방어 (헌법 7장)
            checks.append(_c("D1_champion_challenger", False,
                             f"champion RMSE {before} ≤ 0 — 개선율 산출 불가", critical=True))
        else:
            gain = (before - metrics["rmse_after"]) / before * 100.0
            metrics["rmse_gain_pct"] = round(gain, 3)
            thr = float(gate["promote_min_rmse_gain_pct"])
            checks.append(_c(
                "D1_champion_challenger", gain >= thr,
                f"pooled RMSE {before:.3f} → {metrics['rmse_after']:.3f} "
                f"(개선 {gain:+.2f}% / 기준 {thr}%) · 온셋 포함 참고 "
                f"{metrics['rmse_before_incl_onset']:.3f} → "
                f"{metrics['rmse_after_incl_onset']:.3f}"))
            if gain < thr:
                skip_reasons.append(f"C–C 개선 {gain:.2f}% < {thr}%")
    elif not champ_path:
        # champion 부재. 부트스트랩(진짜 최초 학습)과 사고(포인터 유실·해석 실패)를 가른다 —
        # 후자까지 "무조건 승격"으로 넘기면 champion–challenger 평가 없이 배포되어 헌법
        # 3-3 ① 을 우회한다 (리뷰 B2).
        #   · `--champion none` = **호출자(오케스트레이터)가 재학습 전에 스토어를 확인하고
        #     비교 대상 없음을 단언**한 경우 → 부트스트랩으로 신뢰한다.
        #   · `auto` 로 맡겼는데 해석에 실패 = 게이트가 직접 스토어를 본다. 실물 모델이
        #     있는데 champion 을 못 찾았다면 그건 부트스트랩이 아니라 사고 → **SKIP**.
        asserted = (champion_dir == "none")
        if not asserted and other_models_exist(challenger):
            checks.append(_c("D1_champion_challenger", True,
                             "champion 해석 실패인데 스토어에 다른 모델 존재 — 비교 불가로 "
                             "SKIP (부트스트랩 아님. champion 포인터·glob 점검 필요)"))
            skip_reasons.append("champion 해석 실패(스토어 비어있지 않음) — 비교 불가")
        else:
            checks.append(_c("D1_champion_challenger", True,
                             "champion 부재(최초 학습" +
                             (", 호출자 단언" if asserted else ", 스토어 비어 있음") +
                             ") — 비교 생략, 승격 대상"))
        metrics["champion_challenger_result"] = "NO_CHAMPION"
    elif not ok_ev:
        checks.append(_c("D1_champion_challenger", True,
                         "평가 표본 미달로 비교 생략 (C2 가 SKIP 을 유발한다)"))

    # ── D2: 절대 열화 가드 (D13 — 설계 v2 §4-7 신설) ─────────────────────
    # D1(C–C)은 **상대** 비교다. champion 과 challenger 가 같은 오염(학습 원천·라벨 왜곡·
    # 조립 버그)을 공유하면 gain < 임계 → SKIP → 결함 champion 유지로 수렴하고 상대 지표는
    # 끝까지 침묵한다. AE 라벨 오염 38장이 정확히 이 형태로 재학습·게이트·PM 보고를 전부
    # 통과했다 (헌법 7장). 그래서 개선율과 **무관한** 절대 상한을 하나 세운다.
    #
    # critical=False 인 이유: 누수·계약 위반이 아니라 **품질 미달**이다 — FAIL(사고 경보·
    # 에스컬레이션)이 아니라 SKIP(champion 유지·정상 종료)으로 떨어져야 한다 (설계 §4-7).
    # 분모·임계는 산출 시점에 검증한다 (헌법 7장 — 검증 없는 baseline 은 조용한 기능 정지로
    # 끝난다). ratio ≤ 0 이면 cap ≤ 0 이 되어 **모든 challenger 가 영구 SKIP** 한다.
    ratio = float(gate["abs_rmse_max_ratio"])
    if not (ratio > 0) or not math.isfinite(ratio):
        log.warning("ct1_gate_abs_rmse_max_ratio=%r 는 양의 유한값이 아니다 → 기본값 %s 사용 "
                    "(cap≤0 이면 전 challenger 영구 SKIP)", ratio, DEFAULTS["abs_rmse_max_ratio"])
        ratio = float(DEFAULTS["abs_rmse_max_ratio"])
        gate["abs_rmse_max_ratio"] = ratio                   # 리포트 gate_params 도 실제값으로
    cap = float(lp.BENCH_POOLED) * ratio
    metrics["abs_rmse_cap"] = round(cap, 4)
    chal_rmse = metrics.get("rmse_after")
    if chal_rmse is None:
        checks.append(_c("D2_절대_상한", True,
                         f"challenger 채점 없음(표본 미달·판독 실패) — 상한 대조 생략 "
                         f"(상한 {cap:.3f}). 승격은 C2·D1 이 이미 막는다"))
    else:
        ok_abs = chal_rmse <= cap
        checks.append(_c("D2_절대_상한", ok_abs,
                         f"challenger pooled RMSE {chal_rmse:.3f} ≤ 상한 {cap:.3f} "
                         f"(벤치 {lp.BENCH_POOLED} × {gate['abs_rmse_max_ratio']})" if ok_abs else
                         f"challenger pooled RMSE {chal_rmse:.3f} > 상한 {cap:.3f} "
                         f"(벤치 {lp.BENCH_POOLED} × {gate['abs_rmse_max_ratio']}) — 상대 "
                         f"개선율과 무관하게 승격 불가 ('둘 다 나쁨' 차단)"))
        if not ok_abs:
            skip_reasons.append(f"절대 상한 초과: challenger {chal_rmse:.2f} > {cap:.2f}")

    # champion 대조 — **판정에는 쓰지 않는다.** 매 실행 공짜로 산출되는 `rmse_before` 를
    # 같은 상한과 대조해 리포트에 남기고, 연속 초과의 판정(streak)과 에스컬레이션은
    # 오케스트레이터가 상태 파일로 수행한다 (설계 §4-7 — 단일값 발화 금지: H=1일 pooled
    # RMSE 는 일 변동성이 커서 어려운 wafer 몇 장이 건강한 모델의 하루를 밀 수 있다).
    champ_rmse = metrics.get("rmse_before")
    metrics["champion_over_cap"] = None if champ_rmse is None else bool(champ_rmse > cap)
    if champ_rmse is not None and metrics["champion_over_cap"]:
        log.warning("champion 이 절대 상한 초과: %.3f > %.3f — 연속 초과 판정은 "
                    "오케스트레이터 streak (경보만·교체 금지)", champ_rmse, cap)

    # ── B4: 공정성 전제 (경고 — 판정 미반영, 설계 §10-7) ─────────────────
    champ_has_buffer = bool((champ_mani or {}).get("ct1_window_meta"))
    if champ_path:
        checks.append(_c("B4_champion_규율", True,
                         "champion 도 H 버퍼 규율로 학습됨 (창 메타 보유)" if champ_has_buffer
                         else "⚠ champion 이 H 버퍼 규율 이전 모델 — 첫 비교는 유불리가 "
                              "섞일 수 있다 (설계 §10-7, 리허설 확인 대상)"))

    # 판정 3분기 — **critical 실패만 FAIL**이다 (설계 D10).
    #   FAIL = 게이트 차단(누수·계약 위반 등) → challenger 보존·미배포·에스컬레이션
    #   SKIP = 판정 불가(표본 미달) 또는 승격 기준 미달 → champion 유지, **정상 종료**
    # 이 둘을 뭉치면 "개선폭이 2.9%였다"가 사고 경보로 둔갑해 에스컬레이션이 무의미해진다.
    failed = [c["check"] for c in checks if not c["pass"]]
    critical_failed = [c["check"] for c in checks if not c["pass"] and c["critical"]]
    soft_failed = [c for c in failed if c not in critical_failed]
    if critical_failed:
        verdict = "FAIL"
    elif soft_failed or skip_reasons:
        verdict = "SKIP"
    else:
        verdict = "PASS"
    if "champion_challenger_result" not in metrics or verdict != "PASS":
        metrics["champion_challenger_result"] = (
            "PROMOTED" if verdict == "PASS" else ("SKIP" if verdict == "SKIP" else "BLOCKED"))

    return {
        "gate_verdict": verdict,
        "gate_pass": verdict == "PASS",
        "skip_reasons": skip_reasons,
        "failed_checks": failed,
        "critical_failed": critical_failed,
        "checks": checks,
        "metrics": metrics,
        "challenger": str(challenger.resolve()),
        "bundle_name": challenger.name,          # CT² 리포트와 같은 키 (오케스트레이터 대조)
        "champion": metrics["champion"],
        "eval_data": str(Path(eval_csv).resolve()),
        "gate_params": gate,
        "validated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gate_source": "lean85/validate_lean85.py (설계 D12 위임 — 항목·임계 단일 소스)",
    }


# ── 리포트 영속화 (프로세스 간 공유 JSON — 헌법 7장) ──────────────────────────
def _json_safe(obj):
    """비유한 수치(NaN/Inf) → None. 표준 JSON 보장 (헌법 7장·consumer._finite 와 동일 처방)."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, (int, float, np.integer, np.floating)):
        v = float(obj)
        return (int(obj) if isinstance(obj, (int, np.integer)) else v) if math.isfinite(v) else None
    return obj


REPLACE_ATTEMPTS = 5          # Windows 잠금 대비 재시도 (헌법 7장)
REPLACE_BACKOFF_SEC = 0.05


def _replace_with_retry(tmp: Path, out: Path, attempts=REPLACE_ATTEMPTS,
                        backoff=REPLACE_BACKOFF_SEC) -> None:
    """`os.replace` + 짧은 backoff 재시도 — Windows 는 열린 핸들이 있으면 PermissionError."""
    for i in range(attempts):
        try:
            os.replace(tmp, out)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            log.warning("리포트 교체 잠금 — %.2fs 후 재시도 (%d/%d)", backoff * (2 ** i),
                        i + 1, attempts)
            time.sleep(backoff * (2 ** i))


def write_report(report: dict, out) -> Path:
    """게이트 리포트를 원자 교체로 저장 (오케스트레이터가 읽는 공유물 — 헌법 7장)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(_json_safe(report), ensure_ascii=False, indent=2,
                              allow_nan=False), encoding="utf-8")
    try:
        _replace_with_retry(tmp, out)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return out


def build_argparser():
    """CLI 파서 구성."""
    ap = argparse.ArgumentParser(
        prog="validate_lean85",
        description="CT①(lean85) 자동 검증 게이트 (exit 0=PASS / 2=FAIL / 3=SKIP / 1=오류)")
    ap.add_argument("--challenger", required=True, help="challenger stamp 폴더")
    ap.add_argument("--champion", default="auto",
                    help="champion stamp 폴더 (기본 auto = control/ct1/champion.json → glob 폴백)")
    ap.add_argument("--eval-data", required=True, help="평가셋 raw CSV (조립기 산출)")
    ap.add_argument("--window-meta", default=None,
                    help="창 메타 JSON (기본: challenger manifest 의 ct1_window_meta)")
    ap.add_argument("--pm-log", default=None, help="pm_log.json (기본 자동 탐색)")
    ap.add_argument("--params", default=None, help="params.yaml 경로 override")
    ap.add_argument("--out", default=None,
                    help=f"리포트 JSON (기본 <challenger>/{VALIDATION_REPORT})")
    return ap


def main(argv=None) -> int:
    """CLI 진입점 — 리포트 저장 후 판정을 exit code 로 반환."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s │ %(levelname)-7s │ [ct1.validate] %(message)s")
    a = build_argparser().parse_args(argv)

    wmeta = None
    try:
        if a.window_meta:
            wmeta = json.loads(Path(a.window_meta).read_text(encoding="utf-8"))
            if not isinstance(wmeta, dict):      # json.loads 성공 ≠ dict (헌법 7장)
                raise ValueError(f"--window-meta 가 객체 아님: {type(wmeta).__name__}")
        report = validate(a.challenger, a.eval_data, champion_dir=a.champion,
                          window_meta=wmeta, pm_log=a.pm_log, params_path=a.params)
    except Exception as e:                                # noqa: BLE001 — 실행 오류는 1
        log.error("게이트 실행 오류(판정 불가): %s", e, exc_info=True)
        return EXIT_ERROR

    out = write_report(report, a.out or Path(a.challenger) / VALIDATION_REPORT)
    for c in report["checks"]:
        log.info("  %s %-22s %s", "OK " if c["pass"] else "★FAIL", c["check"], c["detail"])

    v = report["gate_verdict"]
    if v == "PASS":
        log.info("게이트 PASS — %d개 항목 전부 통과 → %s", len(report["checks"]), out)
        return EXIT_PASS
    if v == "SKIP":
        log.warning("게이트 SKIP(판정 불가·승격 기준 미달) — %s → %s (champion 유지)",
                    ", ".join(report["skip_reasons"] or report["failed_checks"]), out)
        return EXIT_SKIP
    log.error("게이트 FAIL — %s → %s (challenger 보존·미배포)",
              ", ".join(report["critical_failed"]), out)
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
