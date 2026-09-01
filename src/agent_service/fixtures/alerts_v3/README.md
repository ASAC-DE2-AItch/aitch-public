# fdc.alert Mock Fixtures (C4-1 Day1)

> 자동 생성물 — `scripts/generate_fixtures.py` 실행으로 재생성. **직접 편집 금지**(seed 고정 결정적).
> 소스: `config/anchor_constants.json`(앵커) + `config/params.yaml`(control_limit_k_sigma).
> 계약: `docs/API_Contract_명세서.md` §3 fdc.alert / 판정 프레임: `docs/Agent_프롬프트_라이브러리_v1.md` §4.

## 매트릭스
- **코어 24** = trigger 4종 × context_score 구간 6종
- **엣지 7** = trigger_signal · reference_suspect · multi-violation · crazy_wafer · regime_signal_only · **BROKEN(의도적 파손 1건)** · temp_drift(온도 가변 축 — 2026-07-26)
- context 게이트(B7): `context_score < 31` = Agent 미가동(기록만). `below_gate(22)`가 이 경로.
- ⚠️ 코어는 **조합형 커버리지**(파서·게이트 분기 검증 우선) — 물리적 대표성보다 필드 커버리지 목적.

## 검증
`python scripts/validate_fixtures.py` → 전체 중 **BROKEN 1건만 실패**하면 통과.

## 케이스표

| # | 파일 | trigger | band | score | gate | chamber | 시그니처 | 기대 verdict | 유효 | 비고 |
|---|------|---------|------|-------|------|---------|----------|--------------|------|------|
| 1 | `01_baseline_aging_below_gate.json` | baseline_aging | below_gate | 22 | CLOSED(기록만) | SIM_CH_1 | C11/N3 settled | baseline_aging → limit_option | ✅ | - |
| 2 | `02_baseline_aging_gate_edge.json` | baseline_aging | gate_edge | 31 | OPEN | SIM_CH_2 | C9/N3 settled | baseline_aging → limit_option | ✅ | - |
| 3 | `03_baseline_aging_low.json` | baseline_aging | low | 38 | OPEN | SIM_CH_3 | C62/N3 settled | baseline_aging → limit_option | ✅ | - |
| 4 | `04_baseline_aging_warning.json` | baseline_aging | warning | 48 | OPEN | SIM_CH_4 | C11/N3 settled | baseline_aging → limit_option | ✅ | - |
| 5 | `05_baseline_aging_mid.json` | baseline_aging | mid | 55 | OPEN | SIM_CH_1 | C9/N3 settled | baseline_aging → limit_option | ✅ | - |
| 6 | `06_baseline_aging_elevated.json` | baseline_aging | elevated | 66 | OPEN | SIM_CH_2 | C62/N3 settled | baseline_aging → limit_option | ✅ | - |
| 7 | `07_baseline_aging_upper.json` | baseline_aging | upper | 73 | OPEN | SIM_CH_3 | C11/N3 settled | baseline_aging → limit_option | ✅ | - |
| 8 | `08_baseline_aging_high.json` | baseline_aging | high | 84 | OPEN | SIM_CH_4 | C9/N3 settled | baseline_aging → limit_option | ✅ | - |
| 9 | `09_baseline_aging_severe.json` | baseline_aging | severe | 90 | OPEN | SIM_CH_1 | C62/N3 settled | baseline_aging → limit_option | ✅ | - |
| 10 | `10_baseline_aging_critical.json` | baseline_aging | critical | 95 | OPEN | SIM_CH_2 | C11/N3 settled | baseline_aging → limit_option | ✅ | - |
| 11 | `11_process_shift_below_gate.json` | process_shift | below_gate | 22 | CLOSED(기록만) | SIM_CH_3 | C16(→knob C5)/N3 | process_shift → recipe_option | ✅ | - |
| 12 | `12_process_shift_gate_edge.json` | process_shift | gate_edge | 31 | OPEN | SIM_CH_4 | C31(→knob C1)/N3 | process_shift → recipe_option | ✅ | - |
| 13 | `13_process_shift_low.json` | process_shift | low | 38 | OPEN | SIM_CH_1 | C15(→knob C4)/N3 | process_shift → recipe_option | ✅ | - |
| 14 | `14_process_shift_warning.json` | process_shift | warning | 48 | OPEN | SIM_CH_2 | C16(→knob C5)/N3 | process_shift → recipe_option | ✅ | - |
| 15 | `15_process_shift_mid.json` | process_shift | mid | 55 | OPEN | SIM_CH_3 | C31(→knob C1)/N3 | process_shift → recipe_option | ✅ | - |
| 16 | `16_process_shift_elevated.json` | process_shift | elevated | 66 | OPEN | SIM_CH_4 | C15(→knob C4)/N3 | process_shift → recipe_option | ✅ | - |
| 17 | `17_process_shift_upper.json` | process_shift | upper | 73 | OPEN | SIM_CH_1 | C16(→knob C5)/N3 | process_shift → recipe_option | ✅ | - |
| 18 | `18_process_shift_high.json` | process_shift | high | 84 | OPEN | SIM_CH_2 | C31(→knob C1)/N3 | process_shift → recipe_option | ✅ | - |
| 19 | `19_process_shift_severe.json` | process_shift | severe | 90 | OPEN | SIM_CH_3 | C15(→knob C4)/N3 | process_shift → recipe_option | ✅ | - |
| 20 | `20_process_shift_critical.json` | process_shift | critical | 95 | OPEN | SIM_CH_4 | C16(→knob C5)/N3 | process_shift → recipe_option | ✅ | - |
| 21 | `21_equipment_fault_below_gate.json` | equipment_fault | below_gate | 22 | CLOSED(기록만) | SIM_CH_1 | C62/N1 CRITICAL | equipment_fault → manual_option | ✅ | - |
| 22 | `22_equipment_fault_gate_edge.json` | equipment_fault | gate_edge | 31 | OPEN | SIM_CH_2 | C31/N1 CRITICAL | equipment_fault → manual_option | ✅ | - |
| 23 | `23_equipment_fault_low.json` | equipment_fault | low | 38 | OPEN | SIM_CH_3 | C32/N1 CRITICAL | equipment_fault → manual_option | ✅ | - |
| 24 | `24_equipment_fault_warning.json` | equipment_fault | warning | 48 | OPEN | SIM_CH_4 | C62/N1 CRITICAL | equipment_fault → manual_option | ✅ | - |
| 25 | `25_equipment_fault_mid.json` | equipment_fault | mid | 55 | OPEN | SIM_CH_1 | C31/N1 CRITICAL | equipment_fault → manual_option | ✅ | - |
| 26 | `26_equipment_fault_elevated.json` | equipment_fault | elevated | 66 | OPEN | SIM_CH_2 | C32/N1 CRITICAL | equipment_fault → manual_option | ✅ | - |
| 27 | `27_equipment_fault_upper.json` | equipment_fault | upper | 73 | OPEN | SIM_CH_3 | C62/N1 CRITICAL | equipment_fault → manual_option | ✅ | - |
| 28 | `28_equipment_fault_high.json` | equipment_fault | high | 84 | OPEN | SIM_CH_4 | C31/N1 CRITICAL | equipment_fault → manual_option | ✅ | - |
| 29 | `29_equipment_fault_severe.json` | equipment_fault | severe | 90 | OPEN | SIM_CH_1 | C32/N1 CRITICAL | equipment_fault → manual_option | ✅ | - |
| 30 | `30_equipment_fault_critical.json` | equipment_fault | critical | 95 | OPEN | SIM_CH_2 | C62/N1 CRITICAL | equipment_fault → manual_option | ✅ | - |
| 31 | `31_escalate_below_gate.json` | escalate | below_gate | 22 | CLOSED(기록만) | SIM_CH_3 | C17(regime)/N3 | escalate → regime_signal | ✅ | - |
| 32 | `32_escalate_gate_edge.json` | escalate | gate_edge | 31 | OPEN | SIM_CH_4 | C17(regime)/N3 | escalate → regime_signal | ✅ | - |
| 33 | `33_escalate_low.json` | escalate | low | 38 | OPEN | SIM_CH_1 | C17(regime)/N3 | escalate → regime_signal | ✅ | - |
| 34 | `34_escalate_warning.json` | escalate | warning | 48 | OPEN | SIM_CH_2 | C17(regime)/N3 | escalate → regime_signal | ✅ | - |
| 35 | `35_escalate_mid.json` | escalate | mid | 55 | OPEN | SIM_CH_3 | C17(regime)/N3 | escalate → regime_signal | ✅ | - |
| 36 | `36_escalate_elevated.json` | escalate | elevated | 66 | OPEN | SIM_CH_4 | C17(regime)/N3 | escalate → regime_signal | ✅ | - |
| 37 | `37_escalate_upper.json` | escalate | upper | 73 | OPEN | SIM_CH_1 | C17(regime)/N3 | escalate → regime_signal | ✅ | - |
| 38 | `38_escalate_high.json` | escalate | high | 84 | OPEN | SIM_CH_2 | C17(regime)/N3 | escalate → regime_signal | ✅ | - |
| 39 | `39_escalate_severe.json` | escalate | severe | 90 | OPEN | SIM_CH_3 | C17(regime)/N3 | escalate → regime_signal | ✅ | - |
| 40 | `40_escalate_critical.json` | escalate | critical | 95 | OPEN | SIM_CH_4 | C17(regime)/N3 | escalate → regime_signal | ✅ | - |
| 41 | `41_edge_trigger_signal.json` | edge | trigger_signal | 55 | OPEN | SIM_CH_1 | trigger_signal 존재 | baseline_aging(참고) | ✅ | trigger_signal(계약 미정의) 보존 파싱 검증 |
| 42 | `42_edge_reference_suspect.json` | edge | reference_suspect | 70 | OPEN | SIM_CH_2 | tttm.reference_suspect=true | escalate(참조 의심) | ✅ | reference_suspect 참(공통 원인) — 역방향 룰 |
| 43 | `43_edge_multi_violation.json` | edge | multi_violation | 78 | OPEN | SIM_CH_3 | violations 2건(N1+N3) | equipment_fault(급변 우선) | ✅ | 복수 위반 배열 파싱 + max_severity 검증 |
| 44 | `44_edge_crazy_wafer.json` | edge | crazy_wafer | 92 | OPEN | SIM_CH_4 | predicted_c65 1650 > 1572(B9) | equipment_fault + HOLD | ✅ | crazy wafer(B9 초과) — disposition HOLD 후보 |
| 45 | `45_edge_regime_signal_only.json` | edge | regime_signal_only | 60 | OPEN | SIM_CH_1 | SHAP 전부 레짐 신호(C12/C17/C33) | escalate(튜닝 금지) | ✅ | 레짐 신호 단독 — recipe hypothesis=regime_signal |
| 46 | `46_edge_BROKEN.json` | edge | BROKEN | 130 | - | SIM_CH_2 | context_score=130 (범위 초과) | ❌ 검증 실패(의도적) | ❌(의도) | ★의도적 파손 — validate 가 이 1건만 실패시켜야 통과 |
| 47 | `47_process_shift_temp_drift.json` | process_shift | temp_drift | 64 | OPEN | SIM_CH_3 | C17/N3 settled · C12 미동반 | process_shift → recipe_option(temp_target) | ✅ | 온도 가변 축(2026-07-26 멘토 확정) — 급변·C12 없는 온도 추세는 temp_target 튜닝 |
