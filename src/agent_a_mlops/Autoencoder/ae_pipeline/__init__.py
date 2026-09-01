"""
ae_pipeline — ae_v3 최종 AE 트랙 재학습·추론 패키지 (A트랙·세그).

집계 MLP-AE(z16 rev3) + 조건화 잔차 Mahalanobis 스코어. 단일 `ae_raw` 계약(불가침 12).
per-wafer FDC trace → ae_score(0~1) · ae_drift_score · ae_top_channels.

모듈:
  constants     단일소스 상수·시드·해시 (부록 A)
  features      전처리 유틸 + extract_features (D=83)
  model         MLPAE·train_ae + Scorer (조건화 잔차 Maha)
  calibration   ae_raw → ae_score (ECDF 앵커)
  drift         ae_drift_score (EWMA, per-chamber)
  io_bundle     아티팩트 번들 저장/로드·무결성 해시
  retrain       CT② 전체 재학습 파이프라인 + 입력 스키마 어댑터 (CLI)
  validate_bundle  CT² 자동 검증 게이트 — 항목·임계 단일 소스 (CLI, 헌법 3-3 ③ ⓑ)
  infer         추론·스코어링 + build-scorer (CLI)
"""
__version__ = "1.0.0"
MODEL_VERSION = "z16_rev3_seg1"   # 최종 확정 모델 (ae_v3)
CALIB_VERSION = "calib_v1"
