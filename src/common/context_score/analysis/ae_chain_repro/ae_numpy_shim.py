"""
ae_numpy_shim.py — torch/sklearn 없는 환경에서 ae_v3 번들을 재현하는 numpy 구현.

목적: ae_raw → ae_score → ae_drift_score 체인을 실데이터로 재현해 직관 자료를 만든다.
검증: 재현한 ae_raw의 VAL 분위수 [50,90,98.5,99.5,99.9]가 calib.json의 anchors_x
      [36.90, 88.46, 179.45, 254.88, 399.00]와 일치하면 전 체인이 원본과 동일하다.

원본 대응:
  ae_pipeline/model.py  MLPAE / build_scorer / Scorer.ae_raw
  sklearn QuantileTransformer(output_distribution='normal').transform
  sklearn.covariance.LedoitWolf
"""
from __future__ import annotations

import io
import math
import pickle
import zipfile
from statistics import NormalDist

import numpy as np

# ── 1. torch .pt(state_dict) 로더 ──────────────────────────────────────────

_TORCH_DTYPE = {
    "FloatStorage": np.float32, "DoubleStorage": np.float64,
    "HalfStorage": np.float16, "LongStorage": np.int64,
    "IntStorage": np.int32, "ShortStorage": np.int16,
    "CharStorage": np.int8, "ByteStorage": np.uint8,
    "BoolStorage": np.bool_,
}


class _StorageStub:
    def __init__(self, name):
        self.name = name


def _rebuild_tensor_v2(storage, storage_offset, size, stride, *a):
    key, dtype, numel = storage
    return ("TENSOR", key, dtype, storage_offset, tuple(size), tuple(stride))


class _PtUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module.startswith("torch"):
            if name == "_rebuild_tensor_v2":
                return _rebuild_tensor_v2
            if name.endswith("Storage"):
                return _StorageStub(name)
            return lambda *a, **k: None
        return super().find_class(module, name)

    def persistent_load(self, pid):
        # ('storage', StorageStub, key, location, numel)
        _, stub, key, _loc, numel = pid
        name = stub.name if isinstance(stub, _StorageStub) else str(stub)
        return (str(key), _TORCH_DTYPE.get(name, np.float32), int(numel))


def load_state_dict(pt_path) -> dict:
    """torch.save로 저장된 state_dict(.pt) → {이름: np.ndarray}."""
    zf = zipfile.ZipFile(pt_path)
    root = zf.namelist()[0].split("/")[0]
    obj = _PtUnpickler(io.BytesIO(zf.read(f"{root}/data.pkl"))).load()

    out = {}
    for name, t in obj.items():
        _, key, dtype, offset, size, stride = t
        buf = zf.read(f"{root}/data/{key}")
        flat = np.frombuffer(buf, dtype=dtype)
        arr = np.lib.stride_tricks.as_strided(
            flat[offset:], shape=size,
            strides=tuple(s * flat.dtype.itemsize for s in stride),
        )
        out[name] = np.array(arr, dtype=np.float64)
    return out


# ── 2. MLP-AE 순전파 (numpy) ───────────────────────────────────────────────

_erf = np.frompyfunc(math.erf, 1, 1)


def gelu(x):
    """nn.GELU() 기본(정확형): x * 0.5 * (1 + erf(x/sqrt(2)))."""
    return x * 0.5 * (1.0 + _erf(x / math.sqrt(2.0)).astype(np.float64))


def linear(x, W, b):
    return x @ W.T + b


def layernorm(x, w, b, eps=1e-5):
    m = x.mean(-1, keepdims=True)
    v = x.var(-1, keepdims=True)          # biased (torch 기본)
    return (x - m) / np.sqrt(v + eps) * w + b


def ae_forward(sd: dict, X: np.ndarray) -> np.ndarray:
    """MLPAE.forward — dec(enc(x)). eval 모드이므로 Dropout은 항등."""
    h = linear(X, sd["enc.0.weight"], sd["enc.0.bias"])
    h = gelu(h)
    h = layernorm(h, sd["enc.2.weight"], sd["enc.2.bias"])
    h = linear(h, sd["enc.4.weight"], sd["enc.4.bias"])
    h = gelu(h)
    h = layernorm(h, sd["enc.6.weight"], sd["enc.6.bias"])
    z = linear(h, sd["enc.7.weight"], sd["enc.7.bias"])

    h = linear(z, sd["dec.0.weight"], sd["dec.0.bias"])
    h = gelu(h)
    h = layernorm(h, sd["dec.2.weight"], sd["dec.2.bias"])
    h = linear(h, sd["dec.3.weight"], sd["dec.3.bias"])
    h = gelu(h)
    h = layernorm(h, sd["dec.5.weight"], sd["dec.5.bias"])
    return linear(h, sd["dec.6.weight"], sd["dec.6.bias"])


def ae_resid(sd, X):
    """r = x − AE(x)."""
    return np.asarray(X, float) - ae_forward(sd, np.asarray(X, float))


# ── 3. QuantileTransformer(normal).transform (numpy) ───────────────────────

class _SkStub:
    """sklearn 클래스 자리를 채우는 껍데기 (속성만 복원)."""

    def __init__(self, *a, **k):
        pass

    def __setstate__(self, state):
        self.__dict__.update(state if isinstance(state, dict) else {})


class _SkUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module.startswith("sklearn"):
            return type(name, (_SkStub,), {})
        return super().find_class(module, name)


def load_quantile_transformer(pkl_path):
    """scaler.pkl → (quantiles_, references_) 만 뽑아온다."""
    with open(pkl_path, "rb") as f:
        obj = _SkUnpickler(f).load()
    return np.asarray(obj.quantiles_, float), np.asarray(obj.references_, float)


_ndtri = np.frompyfunc(NormalDist().inv_cdf, 1, 1)
_BOUNDS = 1e-7


def quantile_transform_normal(X, quantiles, references):
    """sklearn QuantileTransformer(output_distribution='normal').transform 재현."""
    X = np.array(X, dtype=float, copy=True)
    out = np.empty_like(X)
    lo_clip = NormalDist().inv_cdf(_BOUNDS - np.spacing(1))
    hi_clip = NormalDist().inv_cdf(1 - (_BOUNDS - np.spacing(1)))

    for j in range(X.shape[1]):
        col = X[:, j]
        q = quantiles[:, j]
        lower_x, upper_x = q[0], q[-1]
        lower_idx = col == lower_x
        upper_idx = col == upper_x
        finite = ~np.isnan(col)
        cf = col[finite]
        # 양방향 보간 평균 (동점 구간 처리 — sklearn 원 구현)
        y = 0.5 * (np.interp(cf, q, references)
                   - np.interp(-cf, -q[::-1], -references[::-1]))
        newcol = np.empty_like(col)
        newcol[finite] = y
        newcol[~finite] = np.nan
        newcol[upper_idx] = 1.0
        newcol[lower_idx] = 0.0
        newcol = np.clip(newcol, _BOUNDS - np.spacing(1),
                         1 - (_BOUNDS - np.spacing(1)))
        newcol = _ndtri(newcol).astype(np.float64)
        out[:, j] = np.clip(newcol, lo_clip, hi_clip)
    return out


# ── 4. LedoitWolf (numpy) ─────────────────────────────────────────────────

def ledoit_wolf_cov(X):
    """sklearn.covariance.LedoitWolf().fit(X).covariance_ 재현."""
    X = np.asarray(X, float)
    n, p = X.shape
    Xc = X - X.mean(0)
    X2 = Xc ** 2
    emp_trace = X2.sum(0) / n           # 대각 (feature별 분산)
    mu = emp_trace.sum() / p

    beta_raw = float(np.sum(X2.T @ X2))
    delta_raw = float(np.sum((Xc.T @ Xc) ** 2)) / n ** 2   # ||S||_F^2

    beta = (1.0 / (p * n)) * (beta_raw / n - delta_raw)
    delta = (delta_raw - 2.0 * mu * emp_trace.sum() + p * mu ** 2) / p
    beta = min(beta, delta)
    shrinkage = 0.0 if delta == 0 else beta / delta

    S = (Xc.T @ Xc) / n
    cov = (1.0 - shrinkage) * S
    cov.flat[:: p + 1] += shrinkage * mu
    return cov, shrinkage


# ── 5. Scorer (조건화 잔차 Mahalanobis²) ───────────────────────────────────

class NpScorer:
    """model.py Scorer의 numpy판. ae_raw = (r−μ)ᵀ P (r−μ)."""

    def __init__(self, sd, mu, sdev, P, eff_rank=None, cond_used=None):
        self.sd, self.mu, self.sdev, self.P = sd, mu, sdev, P
        self.eff_rank, self.cond_used = eff_rank, cond_used

    @classmethod
    def build(cls, sd, X_val, cond_cap=100.0):
        rv = ae_resid(sd, X_val)
        mu = rv.mean(0)
        sdev = rv.std(0) + 1e-9
        cov, _ = ledoit_wolf_cov(rv)
        wv, V = np.linalg.eigh(cov)
        wv_f = np.maximum(wv, wv.max() / cond_cap)
        P = (V / wv_f) @ V.T
        eff = float((wv.sum() ** 2) / (wv ** 2).sum())
        return cls(sd, mu, sdev, P, eff, float(wv_f.max() / wv_f.min()))

    def ae_raw(self, X):
        d = ae_resid(self.sd, X) - self.mu
        return np.einsum("ij,jk,ik->i", d, self.P, d)

    def top_channels(self, X, feat_cols, w_vec, k=5):
        z = w_vec * ((ae_resid(self.sd, X) - self.mu) / self.sdev) ** 2
        return [[feat_cols[j] for j in row[::-1]] for row in np.argsort(z, 1)[:, -k:]]
