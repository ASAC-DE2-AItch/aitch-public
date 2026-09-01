import { paramsView } from "../../mock/fdc";
import ThemeSeg, { type ThemeMode } from "../ThemeSeg";

/** Settings — 테마·버전·파라미터 스냅샷 (정본: config/params.yaml — 하드코딩 금지 원칙) */
export default function SettingsScreen({
  theme, onTheme, dark,
}: { theme: ThemeMode; onTheme: (t: ThemeMode) => void; dark: boolean }) {
  return (
    <div className="scr">
      <div className="scrh">
        <div>
          <div className="t">Settings</div>
          <div className="c num">테마 · 버전 · CONFIG 스냅샷 (읽기 전용)</div>
        </div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr 1fr" }}>
        <div className="col">
          <div className="card">
            <div className="chead"><span className="t">테마</span></div>
            <div className="krow">
              <b>블루프린트 {theme === "system" ? `시스템 (현재 ${dark ? "다크" : "라이트"})` : dark ? "다크" : "라이트"}</b>
              <span style={{ marginLeft: "auto" }}><ThemeSeg theme={theme} onTheme={onTheme} /></span>
            </div>
            <div className="scrfoot num" style={{ padding: "0 16px 12px" }}>시스템 = OS 설정 추종 (기본) · 라이트 = 시연 테마 (정의서 v1.5) · 다크 = #0a1626 심해 네이비</div>
          </div>
          <div className="card">
            <div className="chead"><span className="t">버전</span></div>
            {[
              // 2026-08-03 정정 — 아래 5행은 전부 근거를 붙였다. 옛 값의 문제:
              //  · "FDC+시간 99피처" → 2026-07-22 에 팀이 공식 철회한 오기다.
              //    정본(일정_v5·스토리보드): 예측 = lean-85(85피처), AE = 83피처,
              //    "'99' 피처 세트는 실재하지 않음". 발표 12일 전까지 화면이 이걸 띄우고 있었다.
              //  · "RMSE 76.5" → 공식 헤드라인은 76.543 이고 컷 단서와 반드시 세트(수치 규칙 v2).
              //  · "AE 99 통일 검증 중" → 실제 열린 과제는 A-AE99 = AE 83 ↔ 예측 85 정합 검증.
              //  · "qwen3:30b" → 실서빙은 QuantTrio/Qwen3.6-35B-A3B-AWQ (params.yaml vllm_model).
              //  · "API Contract v4.6" → 정본은 v4.6-d (2026-07-30, §8-E 배포 자동화 개정).
              //  · (2026-08-05, #99 A 리뷰) 예측·AE 두 행을 A 제공 정본 문구로 재교체 —
              //    76.543(정적 80:19 컷)은 운영 전제와 맞는 시간축 pooled 70.98 · honest R² 0.8545 로,
              //    AE 는 운영 번들 실명(ae_v3 z16_rev3_seg1 · calib_v1)으로 현행화.
              //  · (2026-08-10, #99 후속) 네 행을 **실값 대조로 재검증**해 교체했다. 화면이 뒤처진 것:
              //    ⓐ RMSE 70.98 단독 → 최종값 **99.84(자 = 모델 결정 시점·롤링 미적용)** 를 앞에 두고
              //       70.98 은 **롤링 재학습 전제** 라는 자를 붙여 참고로 병기. 두 값은 서로 다른 자라
              //       어느 한쪽으로 갈음할 수 없다(헌법 7장 "파생 수치는 자와 세트로").
              //    ⓑ AE `ae_v3 z16_rev3_seg1 · calib_v1` → 실 서빙 번들 manifest 값
              //       `z16_rev7_seg1pre_e4 · calib_v7_seg1` (#153 이 compose AE_BUNDLE_DIR 기본값을 e4 로 고정).
              //    ⓒ 관리선 "v1 · v13" → DB 실측은 **v1(initial) 180행**뿐이다(v13 은 현재 활성에 없다).
              //    ⓓ API Contract v4.6-d → 명세서 헤더 실값 **v4.6-f**.
              //    검증 출처: `docker-compose.yml:464` AE_BUNDLE_DIR · 번들 `manifest.json` ·
              //    `control_limits` is_active 조회 · 명세서 3행. 추정으로 적은 값은 한 줄도 없다.
              ["예측 모델", "XGBoost · lean-85 (FDC+시간 85피처) · 최종 RMSE 99.84 · R² 0.85 [자 = 모델 결정 시점 · 롤링 미적용] (baseline 129.750 · R² −0.27 → −29.9pt) · 참고 = 롤링 재학습 전제 시간축 pooled 70.98 · honest R² 0.8545 (무재학습 254.9) — 자가 달라 최종값과 섞지 않는다"],
              ["AE", "z16_rev7_seg1pre_e4 · calib_v7_seg1 · MLP-AE(D→64→32→z16) · 83피처 · qual 0.2 = VAL P98.5 앵커 · 서빙 번들 ae_cand_seg1pre_e4_20260806 (compose AE_BUNDLE_DIR 기본값 · #153)"],
              ["관리선", "is_active = v1 · trigger_type initial · 180행 (effective_from 2026-08-08 재시딩 배치 — CSV + 챔버 오프셋이 단일 소스)"],
              ["API Contract", "v4.6-f (2026-08-07 — §2 bias_applied · §0 consumer-group-bias 신설)"],
              ["임베딩·생성", "bge-m3 · QuantTrio/Qwen3.6-35B-A3B-AWQ (vLLM)"],
            ].map(([k, v]) => (
              <div className="krow" key={k}><b style={{ width: 90 }}>{k}</b><span className="num" style={{ color: "var(--label)", fontSize: 11 }}>{v}</span></div>
            ))}
          </div>
        </div>

        <div className="card">
          <div className="chead"><span className="t">파라미터 스냅샷</span><span className="cap num">정본 = config/params.yaml</span></div>
          {paramsView.map((p) => (
            <div className="krow" key={p.k}>
              <span style={{ fontSize: 12 }}>{p.k}</span>
              <b className="num" style={{ marginLeft: "auto" }}>{p.v}</b>
            </div>
          ))}
          <div className="scrfoot num" style={{ padding: "4px 16px 12px" }}>수정은 화면이 아니라 params.yaml PR로 (합의안 v1 근거 필수)</div>
        </div>

        <div className="col">
          <div className="card">
            <div className="chead"><span className="t">Agent 게이트 3중화</span><span className="cap num">2026-07-14 PM 확정</span></div>
            {[
              ["① 자동", "context_score ≥ 31 (B7)"],
              ["② 즉시", "severity = critical (점수 무관)"],
              ["③ 수동", "엔지니어 'AI 분석 요청' — trigger_type='manual' · RAG 태깅"],
            ].map(([k, v]) => (
              <div className="krow" key={k}><b style={{ width: 56 }}>{k}</b><span style={{ fontSize: 12, color: "var(--label)" }}>{v}</span></div>
            ))}
          </div>
          <div className="card">
            <div className="chead"><span className="t">불변 원칙 (헌법 요약)</span></div>
            {[
              "승인 없는 실력치·Recipe·정비·Disposition 변경 금지",
              "fdc.alert 단일 경보 채널 — 우회 호출 금지",
              "C65 feature 사용 금지 · Wafer 그룹 분할",
              "Incident당 동시 pending 1건",
              "센서는 C코드 — 표시 계층만 발표명",
            ].map((t) => (
              <div className="krow" key={t}><span className="dot" style={{ width: 5, height: 5, background: "var(--faint)" }} /><span style={{ fontSize: 12 }}>{t}</span></div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
