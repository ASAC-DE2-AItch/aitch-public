# -*- coding: utf-8 -*-
"""멀티챔버 복제 엔진 — P3-1 (E1=6대, E3=오프셋 ±0.5σ).

실데이터(단일 챔버)를 가상 챔버 N대로 복제한다:
  - 챔버별 고정 오프셋: 센서별 std × U(-offset_sigma, +offset_sigma) — 시드 고정(재현성)
  - 행 단위 노이즈: **(recipe, step, C42)별** std × N(0, noise_sigma) — 전역 std 는 스텝 계단·점화 과도에서 수백 배 과대 (2026-07-30)
  - wafer 단위 라운드로빈 분배 (챔버들이 병렬로 wafer를 처리하는 효과)
  - 챔버별 pm_count: C33(RF time) 리셋 감지로 증가

wafer_id 규칙: 원본 wafer_id에 챔버 접미사 부여 (예: C64_516_CH3) — 챔버 간 중복 방지.
"""

import logging

import numpy as np
import pandas as pd

log = logging.getLogger("chamber-replicator")

# 오프셋·노이즈를 적용하지 않는 컬럼 (식별자·플래그·카운터·시간류)
NON_PERTURB_COLUMNS = {
    "C6", "C7", "C10", "C20", "C22", "C23", "C33", "C34", "C39",
    "C41", "C42", "C46", "C50", "C59", "C60", "C64",
}


class ChamberReplicator:
    """단일 챔버 실데이터 → 가상 챔버 N대 스트림."""

    def __init__(self, df: pd.DataFrame, chamber_count: int,
                 offset_sigma: float, noise_sigma: float = 0.05, seed: int = 42,
                 stats_df: "pd.DataFrame | None" = None):
        """df는 원본 순서(시간순) 그대로여야 한다.

        stats_df: std·offset 계산용 모집단(기본=df). replay는 seg1로 좁히되(df) 통계는 전체로
        유지하려면 stats_df=전체 df를 넘긴다 — chamber_offsets(B 시딩) 재현성 보존
        (PM 결정 2026-07-28: replay만 seg1, offset std는 불변 — C17 오탐 수정, offset·재시딩 무영향).
        """
        self.df = df
        self.chamber_count = chamber_count
        self.rng = np.random.default_rng(seed)
        stats = stats_df if stats_df is not None else df

        # 센서별 std (수치 컬럼만) — stats(전체 모집단) 기준: offset 스케일 재현성 소스
        numeric = stats.select_dtypes(include=[np.number]).columns
        self._std = {
            c: float(stats[c].std()) for c in numeric
            if c not in NON_PERTURB_COLUMNS and stats[c].std() > 0
        }

        # 챔버별 고정 오프셋 (E3: ±offset_sigma × std 내 균등)
        self.offsets: dict[str, dict[str, float]] = {}
        for i in range(1, chamber_count + 1):
            ch = f"SIM_CH_{i}"
            self.offsets[ch] = {
                c: float(self.rng.uniform(-offset_sigma, offset_sigma) * s)
                for c, s in self._std.items()
            }
        self.noise_sigma = noise_sigma

        # (recipe, step, C42)별 std — **노이즈 스케일 전용** (2026-07-30 수정).
        #   구현이 전역 std 하나로 노이즈를 만들었는데, 스텝 계단 센서(C62 등)는 전역 std가
        #   step 간 격차를 재서 step 내 변동의 수백 배가 된다 → 매 행 ~35σ 노이즈 → 관리선
        #   전건 이탈 (리허설 실측 2026-07-30: 3시간 위반 41k, N1 72%, 4챔버 동일).
        #   C42(과도/정착)까지 내리는 이유: step 4 는 플라즈마 점화라 과도 std(2,367)가
        #   정착 std(24.9)의 100배 — B 관리선은 sensor_window 별로 잡히므로 노이즈도 같은
        #   축으로 재야 한다. 실측 정합: 정착 노이즈 0.05×std 가 전 step 에서 B σ의 0.1σ 이내.
        #   (근거 = 데이터 사전 분석 원칙 — "전체 데이터 상관은 스텝별 계단이 만드는 허상")
        # ⚠️ 오프셋은 위(전역 std) **불변** — 챔버 정체성이자 B 시딩 재현성 소스 (7/28
        #   seg1 결정과 동일 패턴). rng 소비도 오프셋 루프가 먼저라 여기 추가 계산(pandas,
        #   rng 무소비)은 오프셋 값을 바꾸지 않는다 — 검증: 신구 오프셋 전 센서 일치 실측.
        self._std_rsw: dict[tuple, float] = {}               # (recipe, step, c42, col)
        self._std_sw: dict[tuple, float] = {}                # (step, c42, col) 폴백 — C6_1 소표본
        if {"C6", "C7", "C42"} <= set(stats.columns):
            g1 = stats.groupby(["C6", "C7", "C42"], sort=False)[list(self._std)].std()
            self._std_rsw = {(str(r), int(st), int(w), c): float(v)   # 키 = msg 표현(str/int)
                             for (r, st, w), row in g1.iterrows()
                             for c, v in row.items() if pd.notna(v) and v > 0}
            g2 = stats.groupby(["C7", "C42"], sort=False)[list(self._std)].std()
            self._std_sw = {(int(st), int(w), c): float(v) for (st, w), row in g2.iterrows()
                            for c, v in row.items() if pd.notna(v) and v > 0}

        # ── 가짜 지터 가드 (2026-08-05, 전 컬럼 감사) ──────────────────────────
        #   그룹 std(_std_rsw)는 wafer "간" 격차만으로도 > 0 이 된다. 그런데 그 자로 행
        #   노이즈를 얹으면, 원본에서 wafer "내"가 상수인 채널(이산 상태값 C49 {0,-12,-23,-35},
        #   스텝 상수 C12, C1·C57 settle 등)에 존재하지 않는 요동이 창조된다. AE의
        #   switch/std류 피처는 학습셋에서 이 요동이 0(std≈0)이라 z 가 발산한다 — 노이즈
        #   0.001 에서도 20/20 포화 (closure_report M2 · 감사 docs/원본데이터_컬럼감사_v1.md:
        #   병리 그룹 24컬럼 96개 / step4 settle 상수 = C49·C12·C4·C5·C1).
        #   규칙: 그룹 내 wafer 셀 std 의 **중앙값이 정확히 0**(= 과반 wafer 가 셀 내 상수)이면
        #   그 그룹의 노이즈 키를 제거한다. 표본 부족(NaN)은 제거하지 않는다(보수).
        #   전역 상수(std=0)가 키 부재로 자동 제외되는 원리를 "셀 내 상수"까지 확장한 것 —
        #   목록 하드코딩이 아니라 매 기동 데이터에서 도출한다. 오프셋(챔버 정체성)은 불변.
        if {"C6", "C7", "C42", "C64"} <= set(stats.columns):
            _cs = stats.groupby(["C6", "C7", "C42", "C64"], sort=False)[list(self._std)].std()
            _dropped = 0
            _wm = _cs.groupby(level=[0, 1, 2]).median()
            for (r, st, w), row in _wm.iterrows():
                for c, v in row.items():
                    k = (str(r), int(st), int(w), c)
                    if k in self._std_rsw and pd.notna(v) and v == 0:
                        del self._std_rsw[k]; _dropped += 1
            _wm2 = _cs.groupby(level=[1, 2]).median()
            for (st, w), row in _wm2.iterrows():
                for c, v in row.items():
                    k = (int(st), int(w), c)
                    if k in self._std_sw and pd.notna(v) and v == 0:
                        del self._std_sw[k]; _dropped += 1
            log.info(f"가짜 지터 가드: 셀 내 상수 그룹의 노이즈 키 {_dropped}개 제거")
        # ── 이산 채널 재양자화 (2026-08-06, B dead-band 진단 후속 — "C는 PM 몫") ──
        #   실제 장비의 이산 판독은 양자화가 노이즈 floor 다 — 연속 가우시안을 더해
        #   격자 밖 값(예: C57 11.03, 격자 0.5)을 만드는 것은 비현실적이며, 분해능보다
        #   좁은 요동이 B 관리선 오탐의 한 뿌리였다(B clean run: C57-s4 31.8%·C31-s5 15.8%).
        #   가짜 지터 가드(위)는 "셀 내 상수" 채널만 막아 이산-요동 채널(C57)이 남았다.
        #   규칙: 채널 유니크 값들의 최소 간격 res 를 구하고, **모든 인접 간격이 res 의
        #   정수배**(±1%)면 격자 채널로 판정 → perturb 에서 델타(오프셋+노이즈)를 res 로
        #   반올림한다. 원본 값이 이미 격자 위라 위상은 자동 보존된다. 연속 채널은 배수
        #   검정에 실패해 자연 제외, C49 류 불균일 상태코드(간격 12·11·12)도 제외되어
        #   상태값을 건드리지 않는다. 목록 하드코딩 없음 — 매 기동 데이터에서 도출 (6-4).
        self._resolution: dict[str, float] = {}
        _res_src = stats if stats_df is not None else df
        for _c in self._std:
            if _c not in _res_src.columns:
                continue
            _u = pd.unique(_res_src[_c].dropna().round(9))
            if len(_u) < 2 or len(_u) > 5000:            # 유니크 과다 = 사실상 연속 → 생략
                continue
            _u.sort()
            _d = pd.Series(_u).diff().dropna()
            _d = _d[_d > 1e-9]
            if _d.empty:
                continue
            _res = float(_d.min())
            _ratio = _d / _res
            if float((_ratio - _ratio.round()).abs().max()) < 0.01:
                self._resolution[_c] = _res
        if self._resolution:
            log.info(f"이산 채널 재양자화: 격자 채널 {len(self._resolution)}개 도출 "
                     f"(예: {sorted(self._resolution.items())[:3]})")

        # ── 주입 σ 자 (2026-07-31 재교정) ─────────────────────────────────────
        # B 의 관리선 σ = **replay 구간(seg1)의 wafer 평균 std** 임이 실측으로 확인됐다:
        #   C11 seg1 wafer-mean 35.96 vs B 35.619 (1.01배) · C17 2.359 vs 2.054 (1.15배)
        # 노이즈용 _std_rsw(전체·row 단위)를 주입에 쓰면 두 축이 다 어긋난다 —
        #   ⓐ 전체에는 리셋 전 레짐이 섞여 std 가 부풀고 (C17 41.7 vs 4.9)
        #   ⓑ row std 는 wafer 내부 변동이라 wafer 간 변동(관리선 자)과 다르다
        # 실측 사고: 스톰 4σ 지정 → 화면 81.8σ (C17, B σ 대비 20배 자).
        # 주입만 이 자를 쓴다. 노이즈는 row 단위가 맞으므로 _std_rsw 유지.
        self._std_inject: dict[tuple, float] = {}            # (recipe, step, c42, col)
        self._std_inject_sw: dict[tuple, float] = {}         # (step, c42, col) 폴백
        if {"C6", "C7", "C42", "C64"} <= set(df.columns):
            cols = [c for c in self._std if c in df.columns]
            wm = df.groupby(["C6", "C7", "C42", "C64"], sort=False)[cols].mean()
            g1 = wm.groupby(level=[0, 1, 2]).std()
            self._std_inject = {(str(r), int(st), int(w), c): float(v)
                                for (r, st, w), row in g1.iterrows()
                                for c, v in row.items() if pd.notna(v) and v > 0}
            g2 = wm.groupby(level=[1, 2]).std()
            self._std_inject_sw = {(int(st), int(w), c): float(v)
                                   for (st, w), row in g2.iterrows()
                                   for c, v in row.items() if pd.notna(v) and v > 0}

        log.info(f"챔버 {chamber_count}대 오프셋 생성 (±{offset_sigma}σ, seed={seed}, "
                 f"stats={'전체' if stats_df is not None else 'replay'}) · "
                 f"노이즈 std (recipe,step,C42)별 {len(self._std_rsw):,}키 · "
                 f"주입 std (replay wafer-mean) {len(self._std_inject):,}키")

        # wafer 경계 인덱스 (df = replay 소스, 원본 순서 보존)
        self._wafer_groups = [g for _, g in df.groupby("C64", sort=False)]
        log.info(f"replay wafer {len(self._wafer_groups):,}장 로드")

    def perturb(self, chamber_id: str, col: str, value: float,
                recipe=None, step=None, c42=None) -> float:
        """챔버 오프셋(전역 std — 불변) + 행 노이즈(해당 recipe·step·C42 의 std — 2026-07-30).

        노이즈 스케일 우선순위: (recipe, step, C42) → (step, C42) 폴백(C6_1 소표본 등)
        → **0 (노이즈 생략)**. 전역 std 로는 떨어지지 않는다 — 스텝 계단 센서에서 ~35σ
        노이즈가 됐던 원인이 그 폴백이다. 키 미전달(레거시 호출)·상수 센서도 생략 —
        잘못된 스케일로 흔드느니 안 흔드는 쪽이 안전.
        """
        s = self._std.get(col)
        if s is None or pd.isna(value):
            return value
        delta = self.offsets[chamber_id][col]
        ns = (self._std_rsw.get((recipe, step, c42, col))
              or self._std_sw.get((step, c42, col)))
        if ns:
            delta += float(self.rng.normal(0.0, self.noise_sigma * ns))
        # 이산 채널: 델타를 분해능 격자로 반올림 (2026-08-06 재양자화 — 위 도출 참조).
        # 원본 value 가 격자 위이므로 델타만 스냅하면 결과도 격자 위다. 분해능 미만의
        # 오프셋·노이즈는 0 으로 사라진다 — 판독계가 그보다 잘게 못 보는 게 실물이다.
        res = self._resolution.get(col)
        if res:
            delta = round(delta / res) * res
        return value + delta

    def local_std(self, col: str, recipe=None, step=None, c42=None):
        """주입 σ 해석용 std — **B 관리선과 같은 자** (replay 구간 wafer 평균 std).

        시나리오의 `magnitude_sigma` 가 화면·B 판정에서 문자 그대로 N σ 로 읽히려면
        관리선 산출식과 같은 자를 써야 한다. B 실측 대조로 확정: seg1(replay) 구간의
        wafer 평균 std (C11 35.96 vs B 35.619 · C17 2.359 vs 2.054).
        우선순위 (recipe,step,C42) → (step,C42) → None(주입 생략 — 전역으로 떨어지지 않는다).
        """
        return (self._std_inject.get((recipe, step, c42, col))
                or self._std_inject_sw.get((step, c42, col)))

    # PM 리셋 판정: C33이 직전 대비 이 비율 미만으로 떨어져야 리셋으로 인정
    # (±1 수준의 자연 요동을 리셋으로 오인하지 않기 위함 — 진짜 리셋은 예: 39 → 1)
    PM_RESET_DROP_RATIO = 0.5

    def stream_wafers(self, loop: bool = False):
        """(chamber_id, wafer_rows(DataFrame), chamber_wafer_index, pm_count) 제너레이터.

        라운드로빈: wafer k를 챔버 (k mod N)+1에 배정 — 전 챔버가 유사 속도로 진행.
        pm_count: 챔버별로 C33 급락(리셋) 감지 시 +1 (요동은 무시 — DROP_RATIO).
        """
        pm_count = {f"SIM_CH_{i}": 0 for i in range(1, self.chamber_count + 1)}
        last_c33 = {f"SIM_CH_{i}": None for i in range(1, self.chamber_count + 1)}
        wafer_idx = {f"SIM_CH_{i}": 0 for i in range(1, self.chamber_count + 1)}

        run = 0
        while True:
            run += 1
            for k, wafer in enumerate(self._wafer_groups):
                ch = f"SIM_CH_{(k % self.chamber_count) + 1}"
                c33 = float(wafer["C33"].iloc[0]) if pd.notna(wafer["C33"].iloc[0]) else None
                if (c33 is not None and last_c33[ch] is not None
                        and c33 < last_c33[ch] * self.PM_RESET_DROP_RATIO):
                    pm_count[ch] += 1
                    log.info(f"🔧 {ch} PM 리셋 감지 (C33 {last_c33[ch]} → {c33}) — pm_count={pm_count[ch]}")
                if c33 is not None:
                    last_c33[ch] = c33
                wafer_idx[ch] += 1
                yield ch, wafer, wafer_idx[ch], pm_count[ch]
            if not loop:
                return
            log.info(f"🔄 리플레이 루프 — Run #{run} 종료, 처음부터 재시작")
