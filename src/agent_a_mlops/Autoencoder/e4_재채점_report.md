# e4 게이트 v2 실물 재채점 리포트 (2026-08-10)

> 대상 번들: `models/anomaly_ae/ae_cand_seg1pre_e4_20260806` · 게이트 schema **v3**
> 근거 문서: `docs/CT2_게이트_기준_개정_v2_2026-08-09.md` §6(예측)·§7-1(명령)·§7-3(기록)
> 실행: 사용자 터미널 **foreground** (torch/WDAC — TSR-0001)
> 데이터: `Data/ae_input_sim_postguard_full.csv` (84MB · git 미추적 — `.git/info/exclude`)
> 산출물: `models/anomaly_ae/ae_cand_seg1pre_e4_20260806/validation_v3_schema3.json` (커밋 0c176b6)

## 실행 로그 전문

```powershell
PS C:\Users\Dell3571\Documents\AITCH> Select-String -Path "C:\Users\Dell3571\Documents\AITCH\Data\ae_input_sim_postguard_full.csv" -Pattern "C64_5973_CH_1" -SimpleMatch | Select-Object -First 1

Data\ae_input_sim_postguard_full.csv:24046:2.5667489803642023,,0.0,38.770666322071136,73.3474355254931,C6_0,1.0,14.0,6.
963360438119537,1544831738.6,-12.623898556272401,-344.8024863083435,,C14_0,4.144630494453393,37.592805300278854,84.4594
9172380644,-0.02486354268766516,1.0,C20_241,C21_0,C22_467,C23_7,SIM_CH_1,-0.04995730127438837,,4.130510588830796,C28_0,
C29_0,C30_0,3.861663246945366,4.222715311370112,26.0,15.0,15.0,1.0,,C38_5973,1544831738.6,2018-12-15 08:55:38.600,0.0,0
.0,,0.0,0.0,1.0,,240.43375861439273,-0.5127887175688509,0.0,14.0,16.176155855882474,,14.721080446506663,,30.65745406406
0477,10.864050232105678,12.005035851833886,395.0,19.0,-191.06830817537724,-3511.2658019818177,592.1142925874672,C64_597
3_CH_1
PS C:\Users\Dell3571\Documents\AITCH> Copy-Item config\params.yaml $env:TEMP\params_e4.yaml
PS C:\Users\Dell3571\Documents\AITCH> notepad $env:TEMP\params_e4.yaml   # ct2_gate_require_labels: true → false 한 줄만
PS C:\Users\Dell3571\Documents\AITCH> cd src\agent_a_mlops\Autoencoder
PS C:\Users\Dell3571\Documents\AITCH\src\agent_a_mlops\Autoencoder> python -m ae_pipeline.validate_bundle `
>>   --bundle ..\..\..\models\anomaly_ae\ae_cand_seg1pre_e4_20260806 `
>>   --data "C:\Users\Dell3571\Documents\AITCH\Data\ae_input_sim_postguard_full.csv" `
>>   --input-schema merged `
>>   --params $env:TEMP\params_e4.yaml `
>>   --out ..\..\..\models\anomaly_ae\ae_cand_seg1pre_e4_20260806\validation_v3_schema3.json
2026-08-10 11:05:53,992 │ WARNING │ [ae.validate] ★게이트 완화 감지 — l5_budget=0.04 (기본 0.02, 방향 up). 항목이 느슨해졌다. 완화는 PM 승인 사항 (헌법 3-3 ③ ⓑ).
2026-08-10 11:05:53,992 │ WARNING │ [ae.validate] ★E0 면제 — require_labels=false. 정답지 없이 통과 가능해지므로 **학습셋 오염 방어가 0**이다 (리스크 K-1). 면제 사실을 리포트에 기록한다.
2026-08-10 11:06:04,983 │ INFO    │ [ae.validate]   OK   A1_번들_7파일_완결           7/7
2026-08-10 11:06:04,983 │ INFO    │ [ae.validate]   OK   A2_manifest_sha256     6/6건 일치
2026-08-10 11:06:04,983 │ INFO    │ [ae.validate]   OK   A3_VAL_세트_해시           got=b5929d2f exp=b5929d2f
2026-08-10 11:06:04,984 │ INFO    │ [ae.validate]   OK   A5_게이트_세트_해시           got=7d7aaa44 exp=7d7aaa44
2026-08-10 11:06:04,984 │ INFO    │ [ae.validate]   OK   D3_게이트_홀드아웃            600장 (calib 미적합)
2026-08-10 11:06:04,984 │ INFO    │ [ae.validate]   OK   D4_적합_표본_하한            600 ≥ 100.0 (calib·scorer 적합)
2026-08-10 11:06:04,984 │ INFO    │ [ae.validate]   OK   A6_세트_행_실존             VAL 600장 + 홀드아웃 600장
2026-08-10 11:06:04,984 │ INFO    │ [ae.validate]   OK   B1_L5_오경보율             3.83% (23/600) ≤ 4% · CI[2.74, 5.34]% · PASS-경계
2026-08-10 11:06:04,984 │ INFO    │ [ae.validate]   OK   B2_recon_RMSE          0.2955 ≤ 1.0
2026-08-10 11:06:04,984 │ INFO    │ [ae.validate]   OK   B3_calib①_VAL평균        0.0555 = 0.05±0.02
2026-08-10 11:06:04,984 │ INFO    │ [ae.validate]   OK   B4_calib②_VAL_P95      0.1621 < 0.2
2026-08-10 11:06:04,985 │ INFO    │ [ae.validate]   OK   D1_B군_표본_하한            600 ≥ 100.0 (min_gate_wafers, 홀드아웃)
2026-08-10 11:06:04,985 │ INFO    │ [ae.validate]   OK   B5_챔버별_L5              4개 챔버 전부 ≤ 0.05 · 최악 SIM_CH_3 4.67% (7/150) ≤ 5% · CI[2.55, 8.39]% · PASS-경계 · 경계 4/4개 챔버 (소표본 — CI 상한이 임계를 넘어 '초과 아님'까지만 판정 가능)
2026-08-10 11:06:04,985 │ INFO    │ [ae.validate]   OK   D2_앵커_간격_비율            min_gap/span=0.1710 ≥ 0.01
2026-08-10 11:06:04,985 │ INFO    │ [ae.validate]   OK   C1_프로브_검출률             0.9019 @ 4σ ≥ 0.9 [가중(w_c·부록A) 판정 · 산술 0.8983 병기]
2026-08-10 11:06:04,985 │ INFO    │ [ae.validate]   OK   C2_프로브_단조성             6σ 0.9600 ≥ 1σ 0.2228 (산술 그리드 — 가중 무관 구조 점검)
2026-08-10 11:06:04,985 │ INFO    │ [ae.validate]   OK   A4_채널가중_정합             scorer.w_vec == feat_weight_vector(부록 A)
2026-08-10 11:06:04,985 │ INFO    │ [ae.validate]   OK   E0_라벨_제공               정답지 미제공 — `ct2_gate_require_labels: false` 로 **명시 면제**. 이 상태에서 학습셋 오염 방어는 0이다 (K-1). 승인 브리프에 적시할 것
2026-08-10 11:06:04,985 │ INFO    │ [ae.validate]   OK   E2_라벨_대칭               학습·검증 라벨 사용 일치 (injection_excluded=False)
2026-08-10 11:06:04,986 │ INFO    │ [ae.validate]      └ SIM_CH_1   n=150  L5   4.00%  평균 0.0565  P95 0.1559  CI[2.08, 7.55]% PASS-경계
2026-08-10 11:06:04,986 │ INFO    │ [ae.validate]      └ SIM_CH_2   n=150  L5   4.00%  평균 0.0560  P95 0.1469  CI[2.08, 7.55]% PASS-경계
2026-08-10 11:06:04,986 │ INFO    │ [ae.validate]      └ SIM_CH_3   n=150  L5   4.67%  평균 0.0573  P95 0.1897  CI[2.55, 8.39]% PASS-경계
2026-08-10 11:06:04,986 │ INFO    │ [ae.validate]      └ SIM_CH_4   n=150  L5   2.67%  평균 0.0523  P95 0.1379  CI[1.20, 5.81]% PASS-경계
2026-08-10 11:06:04,986 │ WARNING │ [ae.validate] ★임계 완화 상태로 판정됨 — 2건 (승인 브리프에 노출 필요, 헌법 3-3 ③ ⓑ)
2026-08-10 11:06:04,986 │ WARNING │ [ae.validate]      └ B1 오경보 예산 (l5_budget): 기본 0.02 → 현재 0.04 — 홀드아웃 오경보율 상한을 원 목표보다 올린 상태
2026-08-10 11:06:04,986 │ WARNING │ [ae.validate]         가드: 시뮬 Scorecard 또는 운영에서 미탐 1건 검출 시 즉시 구기준 복귀 (복귀 목표 = 원 목표 l5 0.02) + 원인 규명 전 재완화 금지
2026-08-10 11:06:04,987 │ WARNING │ [ae.validate]      └ E0 정답지 필수 (require_labels): 기본 True → 현재 False — 정답지 없이 통과 가능 — 학습셋 오염 방어 0 (K-1)
2026-08-10 11:06:04,987 │ WARNING │ [ae.validate] ★경계 라벨 — B1_L5_오경보율 PASS-경계 (CI[2.74, 5.34]% vs 예산 4%). 표본이 작아 '예산 이내'가 확정이 아니다
2026-08-10 11:06:04,987 │ WARNING │ [ae.validate] ★경계 라벨 — B5_챔버별_L5 PASS-경계 (CI[2.55, 8.39]% vs 예산 5%). 표본이 작아 '예산 이내'가 확정이 아니다
2026-08-10 11:06:04,987 │ WARNING │ [ae.validate] ★잔여 리스크 — E1(정답지 대조 검출률) 미실행 — 학습셋 오염을 직접 검사한 항목이 없다. 차기 challenger 부터 상시 라운드로 개통 (문서 §4)
2026-08-10 11:06:04,987 │ WARNING │ [ae.validate] ★잔여 리스크 — 경계 라벨 2종 — 표본이 작아 예산 이내임이 '확정'이 아니다 (CI 반폭 ±1%p 를 넘으려면 홀드아웃 ~1000장 — 문서 §1-5, WP-4)
2026-08-10 11:06:04,987 │ INFO    │ [ae.validate] 게이트 PASS — 19개 항목 전부 통과 (schema v3) → ..\..\..\models\anomaly_ae\ae_cand_seg1pre_e4_20260806\validation_v3_schema3.json
PS C:\Users\Dell3571\Documents\AITCH\src\agent_a_mlops\Autoencoder> echo "exit=$LASTEXITCODE"
exit=0
PS C:\Users\Dell3571\Documents\AITCH\src\agent_a_mlops\Autoencoder> cd ..\..\..
PS C:\Users\Dell3571\Documents\AITCH> python -c "import json;d=json.load(open(r'models\anomaly_ae\ae_cand_seg1pre_e4_20260806\validation_v3_schema3.json',encoding='utf-8'));ab=d['approval_brief'];print('verdict',d['gate_verdict'],'schema',d['gate_schema_version']);print('약화',ab['weakened_count'],'/ 경계',ab['boundary_count']);print('C1',d['probe']['at_gate'],'(산술',d['probe']['at_gate_arithmetic'],')');[print(' -',c['check'],c.get('verdict_band'),'|',c['detail']) for c in d['checks'] if c.get('verdict_band')]"
verdict PASS schema 3
약화 2 / 경계 2
C1 0.9019 (산술 0.8983 )
 - B1_L5_오경보율 PASS-경계 | 3.83% (23/600) ≤ 4% · CI[2.74, 5.34]% · PASS-경계
 - B5_챔버별_L5 PASS-경계 | 4개 챔버 전부 ≤ 0.05 · 최악 SIM_CH_3 4.67% (7/150) ≤ 5% · CI[2.55, 8.39]% · PASS-경계 · 경계 4/4개 챔버 (소표본 — CI 상한이 임계를 넘어 '초과 아님'까지만 판정 가능)
```
## 결론 — §6 예측 대조

**재채점 통과 — §6 예측과 전 항목 일치했습니다.**

| | 예측 | 실측 |
|---|---|---|
| verdict | PASS | **PASS** (schema 3, 19항목) |
| B1 | PASS-경계 [2.74, 5.34] | **3.83% (23/600) · CI[2.74, 5.34]** |
| B5 | 4챔버 PASS-경계 | **4/4** · 최악 CH3 4.67% CI[2.55, 8.39] |
| C1 | 가중 0.9019 | **0.9019** (산술 0.8983) |
| 약화 / 경계 | 2 / 2 | **2 / 2** |
