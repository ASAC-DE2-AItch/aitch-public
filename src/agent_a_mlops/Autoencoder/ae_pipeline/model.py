"""
model.py — MLP-AE 아키텍처·학습 + 단일 ae_raw 스코어 계약.

원 노트북 `ae_v3.ipynb` P4 셀(15·16·17)과 동일 로직.

스코어 계약 (불가침 12 — 단일 `ae_raw` 불변):
  ae_raw = 조건화 잔차 Mahalanobis²
         = (r − μ)ᵀ P (r − μ),  r = x − AE(x)  (채널가중 잔차)
  P = 고유값 플로어로 조건수를 cond_cap(=100) 이하로 캡한 정밀도(precision).
  rev2 아티팩트(C27 스케일 지배 → 조건수 폭주)를 원천 차단하는 처방.

핸드오프 확장(원 노트북 대비): 스코어러 통계(μ, sd, P, W_VEC)를 `scorer.npz`로
영속화한다. 노트북은 매 실행 VAL 잔차로 재계산했지만, 운영 추론은 VAL 없이
번들만으로 스코어링해야 하므로 통계를 아티팩트로 고정한다(수학 동일).
"""
from __future__ import annotations

import numpy as np
import torch
import torch.nn as nn


def resolve_device(device=None) -> str:
    if device is not None:
        return device
    return "cuda" if torch.cuda.is_available() else "cpu"


# ── 아키텍처 (D→64→32→z→32→64→D · GELU·LayerNorm·dropout) ──────────────────
class MLPAE(nn.Module):
    def __init__(self, d: int, z: int = 16, p: float = 0.05):
        super().__init__()
        self.enc = nn.Sequential(
            nn.Linear(d, 64), nn.GELU(), nn.LayerNorm(64), nn.Dropout(p),
            nn.Linear(64, 32), nn.GELU(), nn.LayerNorm(32), nn.Linear(32, z),
        )
        self.dec = nn.Sequential(
            nn.Linear(z, 32), nn.GELU(), nn.LayerNorm(32),
            nn.Linear(32, 64), nn.GELU(), nn.LayerNorm(64), nn.Linear(64, d),
        )

    def forward(self, x):
        return self.dec(self.enc(x))


# 채택 학습 config (rev3, seed 42) — 재학습 시 동일 유지(변수 격리).
ADOPTED_CFG = dict(z=16, p=0.05, lr=1e-3, denoise=0.1, max_ep=400, patience=30)


def train_ae(Xtr, Xval, w_vec, z=16, p=0.05, lr=1e-3, denoise=0.0,
             max_ep=200, patience=20, seed=42, device=None):
    """채널가중 MSE로 MLP-AE 학습. VAL loss 조기종료. Returns (model, best_val, n_epoch)."""
    from .constants import seed_all

    device = resolve_device(device)
    seed_all(seed)
    W_T = torch.tensor(np.asarray(w_vec, float), dtype=torch.float32, device=device)
    m = MLPAE(Xtr.shape[1], z, p).to(device)
    opt = torch.optim.AdamW(m.parameters(), lr=lr)
    sch = torch.optim.lr_scheduler.CosineAnnealingLR(opt, max_ep)
    xt = torch.tensor(Xtr, dtype=torch.float32, device=device)
    xv = torch.tensor(Xval, dtype=torch.float32, device=device)
    best, best_st, bad, ep = 1e9, None, 0, 0
    for ep in range(max_ep):
        m.train()
        perm = torch.randperm(len(xt), device=device)
        for i in range(0, len(xt), 256):
            b = xt[perm[i:i + 256]]
            inp = b + denoise * torch.randn_like(b) if denoise > 0 else b
            loss = (W_T * (m(inp) - b) ** 2).mean()
            opt.zero_grad()
            loss.backward()
            opt.step()
        sch.step()
        m.eval()
        with torch.no_grad():
            vl = float((W_T * (m(xv) - xv) ** 2).mean())
        if vl < best - 1e-6:
            best = vl
            best_st = {k: v.cpu().clone() for k, v in m.state_dict().items()}
            bad = 0
        else:
            bad += 1
        if bad >= patience:
            break
    if best_st is not None:
        m.load_state_dict(best_st)
    m.eval()
    return m, best, ep + 1


def ae_resid(model, X, device=None) -> np.ndarray:
    """채널가중 전 원잔차 r = x − AE(x)."""
    device = resolve_device(device)
    with torch.no_grad():
        xt = torch.tensor(np.asarray(X, float), dtype=torch.float32, device=device)
        return (xt - model(xt)).cpu().numpy()


# ── 스코어 계약 (조건화 잔차 Mahalanobis) ──────────────────────────────────
class Scorer:
    """단일 ae_raw 계약 구현체. model + 고정 통계(μ, sd, P, W_VEC)를 보유."""

    def __init__(self, model, feat_cols, w_vec, mu, sd, P,
                 eff_rank=None, cond_used=None, device=None):
        self.model = model
        self.device = resolve_device(device)
        self.feat_cols = list(feat_cols)
        self.w_vec = np.asarray(w_vec, float)
        self.mu = np.asarray(mu, float)
        self.sd = np.asarray(sd, float)
        self.P = np.asarray(P, float)
        self.eff_rank = None if eff_rank is None else float(eff_rank)
        self.cond_used = None if cond_used is None else float(cond_used)

    def _resid(self, X):
        return ae_resid(self.model, X, self.device)

    def ae_raw(self, X) -> np.ndarray:
        """조건화 잔차 Mahalanobis² (단일 계약, 높을수록 이상)."""
        d = self._resid(X) - self.mu
        return np.einsum("ij,jk,ik->i", d, self.P, d)

    def ae_raw_diag(self, X) -> np.ndarray:
        """(진단) 대각 z제곱합 — 조건양호 하한."""
        d = self._resid(X) - self.mu
        return ((d / self.sd) ** 2).sum(1)

    def recon_rmse(self, X) -> float:
        """채널가중 재구성 RMSE (CT② 정상화 판정용)."""
        r = self._resid(X)
        return float(np.sqrt((self.w_vec * r ** 2).mean()))

    def top_channels(self, X, k: int = 5):
        """잔차 상위 k채널 (C코드) — Evidence Card 재료."""
        z = self.w_vec * ((self._resid(X) - self.mu) / self.sd) ** 2
        return [[self.feat_cols[j] for j in row[::-1]] for row in np.argsort(z, 1)[:, -k:]]

    # ── 영속화 ──
    def save_stats(self, path) -> None:
        np.savez(
            path,
            mu=self.mu, sd=self.sd, P=self.P, w_vec=self.w_vec,
            feat_cols=np.array(self.feat_cols, dtype=object),
            eff_rank=np.array([np.nan if self.eff_rank is None else self.eff_rank]),
            cond_used=np.array([np.nan if self.cond_used is None else self.cond_used]),
        )

    @classmethod
    def load_stats(cls, path, model, device=None) -> "Scorer":
        z = np.load(path, allow_pickle=True)
        eff = float(z["eff_rank"][0]) if "eff_rank" in z else None
        cond = float(z["cond_used"][0]) if "cond_used" in z else None
        return cls(
            model=model,
            feat_cols=list(z["feat_cols"]),
            w_vec=z["w_vec"], mu=z["mu"], sd=z["sd"], P=z["P"],
            eff_rank=None if eff != eff else eff,   # NaN → None
            cond_used=None if cond != cond else cond,
            device=device,
        )


def build_scorer(model, X_val, feat_cols, w_vec, device=None, cond_cap=100.0) -> Scorer:
    """VAL 정상 잔차로 스코어러 통계 산출 (조건수 캡 μ·sd·P).

    P = 고유값 플로어(wv_max/cond_cap)로 조건수를 cond_cap 이하로 캡한 정밀도.
    """
    from sklearn.covariance import LedoitWolf  # 지연 import (추론은 불요)

    device = resolve_device(device)
    rv = ae_resid(model, X_val, device)
    mu = rv.mean(0)
    sd = rv.std(0) + 1e-9
    lw = LedoitWolf().fit(rv)
    wv, V = np.linalg.eigh(lw.covariance_)
    wv_f = np.maximum(wv, wv.max() / cond_cap)      # 고유값 플로어 → 조건수 ≤ cond_cap
    P = (V / wv_f) @ V.T                             # 조건화 정밀도
    eff = float((wv.sum() ** 2) / (wv ** 2).sum())  # 유효차원(participation ratio)
    cond_used = float(wv_f.max() / wv_f.min())
    return Scorer(model, feat_cols, w_vec, mu, sd, P, eff, cond_used, device)


def load_model(state_dict_path, d: int, z: int = 16, p: float = 0.05, device=None) -> MLPAE:
    """state_dict(.pt) → 평가모드 MLPAE."""
    device = resolve_device(device)
    m = MLPAE(d, z, p).to(device)
    state = torch.load(state_dict_path, map_location=device)
    m.load_state_dict(state)
    m.eval()
    return m
