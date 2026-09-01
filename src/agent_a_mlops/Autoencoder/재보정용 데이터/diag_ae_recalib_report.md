# AE 재보정 진단 리포트

- 번들: `models\anomaly_ae\ae_v1`
- 입력: `src\agent_a_mlops\Autoencoder\재보정용 데이터\ae_input_control_v2.csv` / `src\agent_a_mlops\Autoencoder\재보정용 데이터\ae_input_sim_v2.csv`
- 판정: **레짐 불일치 - ae_v1 학습 구간(seg1)과 현재 post-reset 운영 구간(AE 기준 seg2)이 다름**
- 쌍 Spearman: -0.145
- sim recon_rmse: 2.3419 (상한 1.0)

## Frozen 채점

| 세트 | n | P50 | P90 | P98.5 | P99.5 | P99.9 | max | 포화율 | score>qual | 중앙값 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| control | 2000 | 214.6 | 381.3 | 881.5 | 1038.3 | 1240.2 | 1366.8 | 8.70% | 64.85% | 0.3398 |
| sim | 2000 | 2759.4 | 3498.5 | 3904.4 | 4050.8 | 4279.2 | 4631.3 | 100.00% | 100.00% | 1.0 |

### 챔버별

- **control**: SIM_CH_1: n=500, sat=9.4%, >qual=65.4%, P50=214.6 / SIM_CH_2: n=500, sat=8.8%, >qual=67.2%, P50=217.6 / SIM_CH_3: n=500, sat=8.6%, >qual=63.8%, P50=212.7 / SIM_CH_4: n=500, sat=8.0%, >qual=63.0%, P50=215.9
- **sim**: SIM_CH_1: n=500, sat=100.0%, >qual=100.0%, P50=3350.5 / SIM_CH_2: n=500, sat=100.0%, >qual=100.0%, P50=2838.9 / SIM_CH_3: n=500, sat=100.0%, >qual=100.0%, P50=2515.0 / SIM_CH_4: n=500, sat=100.0%, >qual=100.0%, P50=2752.5

## Control 원인 규명

- control의 원본 wafer ID 2,000장을 기존 기준 스코어 파일
  `src/common/context_score/analysis/ae_chain_repro/wafer_scores_all.csv`와 대조했다.
- 기준 파일의 레짐은 **2,000/2,000장 전부 `seg2`**다. 반면 `ae_v1`의 학습·보정
  모집단은 `seg1·C6_0`이다.
- control 재계산 raw와 기준 raw의 Pearson 상관은 **0.999999996**, 최대 절대차는
  **0.0586**, 중앙 절대차는 **0.0085**다. 따라서 feature 추출이나 추론 경로 오류가
  아니라 입력 레짐 불일치다.
- sim recon RMSE 2.3419는 모델 크기나 표현력 부족의 증거가 아니라, `seg1`에 적합된
  scaler·AE weight를 `seg2`에 적용한 결과다. 모델 구조를 확장하지 않고 같은 구조를
  post-reset 정상 데이터에 다시 적합한다.
- 시뮬레이터 문서의 “리셋 후 seg1”과 AE 파이프라인의 레짐 명명에서 같은 구간을 서로
  다르게 부른다. 현재 v2 control은 AE 학습 세계 대조군이 아니므로 수준 1/2 재보정
  판정에 사용할 수 없다.

## 조치

2026-08-02 결정: `replay_post_reset_only: true`를 운영 불변 전제로 승인하고, 현재 서빙
`seg2`를 새 정상 세계로 삼아 **동일 AE 구조의 수준 3 CT2 전체 재학습**으로 전환한다.
