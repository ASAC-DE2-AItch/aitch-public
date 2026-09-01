"""
constants.py — AE(v1 / ae_v3) 단일소스 상수·시드·해시 유틸.

부록 A(채널 단일소스)·불가침 1·7 정의를 코드로 고정한다. 재학습(CT②)·추론이
모두 이 파일 하나만 참조하도록 하여 피처 정의가 노트북과 운영 코드에서 갈라지는
것을 막는다. (원 노트북 `ae_v3.ipynb` P0-2 셀과 바이트 의미 동일)

값을 바꾸면 = 새 피처 계약. feature_spec.json / 모델 / 스케일러를 함께 재산출해야
한다(불가침 12: 단일 ae_raw 계약 불변).
"""
from __future__ import annotations

import hashlib
import random

import numpy as np

# ── 재현성 ────────────────────────────────────────────────────────────────
SEED = 42


def seed_all(s: int = SEED) -> None:
    """random / numpy / torch 시드 고정 (torch 없으면 조용히 통과)."""
    random.seed(s)
    np.random.seed(s)
    try:
        import torch

        torch.manual_seed(s)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(s)
        try:
            torch.use_deterministic_algorithms(True)
        except Exception:
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
    except Exception:
        pass


# ── 컬럼 그룹 (데이터 사전 §6 · 부록 A) ─────────────────────────────────────
STR_COLS = ["C6", "C14", "C20", "C21", "C22", "C23", "C24",
            "C28", "C29", "C30", "C38", "C64", "split"]
CORRUPT_COL = "C40"
DROP_EMPTY = ["C2", "C13", "C26", "C37", "C43", "C47", "C53", "C55"]
DROP_DUP = ["C36", "C35", "C38", "C40"]
CONST_COLS = ["C3", "C8", "C14", "C19", "C21", "C24",
              "C28", "C29", "C30", "C44", "C45", "C51"]
GROUP_KEYS = ["C24", "C6", "C7"]

# 입력 금지 (불가침 1·7): 타깃·식별자·카운터·시간·setpoint·과도플래그·split 등 27종.
# 물리신호만 입력으로 허용한다.
FORBIDDEN_INPUT = set(
    ["C65"]
    + ["C64", "C38", "C20", "C21", "C22", "C23", "C34", "C35", "C14", "C24", "C6", "C7", "C36"]
    + ["C10", "C39", "C40", "C41", "C46", "C33"]
    + ["C1", "C4", "C5", "C48"]
    + ["C12"]
    + ["C42"]
    + ["split"]
)

# ── 부록 A 포함 채널 (물리 신호) ────────────────────────────────────────────
CH_SETTLE_CONT = ["C11", "C17", "C9", "C52", "C15", "C16", "C31", "C63", "C58", "C57"]
CH_TRANSIENT = ["C18", "C27", "C32", "C62", "C61"]
CH_DISCRETE = ["C49", "C54", "C56", "C50"]
CH_MUX = ["C59", "C60"]

# 채널 손실 가중 w_c (부록 A — P4 학습곡선으로 확정). AE MSE·ae_raw z집계에 사용.
CH_LOSS_W = {
    "C11": 1.0, "C17": 1.0, "C9": 1.0, "C52": 0.7, "C15": 1.0, "C16": 1.0,
    "C31": 1.0, "C63": 0.7, "C58": 0.5, "C57": 0.3, "C18": 1.0, "C27": 0.7,
    "C32": 1.0, "C62": 1.0, "C61": 0.7, "C54": 0.7, "C56": 0.7, "C50": 0.5,
    "C49": 0.3, "C5960_active": 0.5,
}
DEFAULT_LOSS_W = 0.5  # 파생·완결성 피처 기본 가중


def feat_weight(col: str) -> float:
    """피처명 → 채널 손실가중 (부록 A 매핑). 매칭 없으면 DEFAULT_LOSS_W."""
    for ch, w in CH_LOSS_W.items():
        if col == ch or col.startswith(ch + "_"):
            return w
    return DEFAULT_LOSS_W


def feat_weight_vector(feat_cols) -> np.ndarray:
    """피처 컬럼 리스트 → 가중 벡터 W_VEC (extract_features 컬럼 순서 그대로)."""
    return np.array([feat_weight(c) for c in feat_cols], dtype=float)


# ── 재현성 해시 지문 ────────────────────────────────────────────────────────
def sha1_8(items) -> str:
    """정렬된 항목 집합의 짧은(8자) SHA1 지문 — 세트 경계 재현성 대조용."""
    h = hashlib.sha1()
    for x in sorted(map(str, items)):
        h.update(str(x).encode())
        h.update(b"|")
    return h.hexdigest()[:8]


# 학습셋(seg1·C6_0) 세트 경계 기대 해시 (ae_eda_v3 REPORT_01 확정 지문).
# CT② 재학습은 새 정상셋이므로 이 값과 다를 수 있음 — 원본 재현 대조용으로만 사용.
EXPECT_HASH = {
    "seg1": "1b142963", "seg2": "0a01fcf4",
    "train": "6ac80376", "val": "fa500f3d", "c6_1": "160c72e8",
}
