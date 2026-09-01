# 초기 실력치 v1 — QA 리포트

- 생성: 2026-08-06T09:04:36.211150+00:00
- 표본: PM 리셋(C64_9664) 이후 복귀 블록 6824장 − seasoning 10장 = 6814장 (2019-01-01 04:07:22.900000095 ~ 2019-02-08 00:42:35.900000095)
- 제외(실험구간): C6_1 93장 · 실험前 C6_0 1330장
- 관리선 그룹 수: 69 / QA 플래그 그룹: 36

## 플래그 요약

- `zero_sigma`: 15건
- `high_false_alarm`: 6건
- `wide_limits`: 11건
- `high_cv`: 7건

## 플래그 상세

| recipe_id   |   step | sensor_window   | sensor_id   |   n_wafers |       center |        sigma |       cv |   false_alarm_pct | qa_flags                                              |
|:------------|-------:|:----------------|:------------|-----------:|-------------:|-------------:|---------:|------------------:|:------------------------------------------------------|
| C6_0        |      1 | settled         | C11         |       6814 |   -1.03581   |   0.124387   |   0.1201 |             6.355 | deadband_floored;high_false_alarm                     |
| C6_0        |      1 | settled         | C15         |       6814 |    2.954     |   3.83467    |   1.2981 |             0     | deadband_floored;wide_limits;high_cv                  |
| C6_0        |      1 | settled         | C16         |       6814 |   39.5537    |  46.2921     |   1.1704 |             0     | wide_limits;high_cv                                   |
| C6_0        |      1 | settled         | C31         |       6814 |    0         |   0          | nan      |             0     | zero_sigma                                            |
| C6_0        |      1 | settled         | C32         |       6814 |    0         |   0          | nan      |             0     | zero_sigma                                            |
| C6_0        |      1 | settled         | C57         |       6814 |   11         |   0          |   0      |             0.044 | zero_sigma;deadband_floored                           |
| C6_0        |      4 | settled         | C15         |       6814 |   59.5812    |  35.9566     |   0.6035 |             0     | wide_limits;high_cv                                   |
| C6_0        |      4 | settled         | C16         |       6814 |  647.963     | 309.126      |   0.4771 |             0     | wide_limits                                           |
| C6_0        |      4 | settled         | C32         |       6814 |    0.627767  |   0.479248   |   0.7634 |             0.426 | wide_limits;high_cv                                   |
| C6_0        |      4 | settled         | C57         |       6814 |   11.3534    |   0.56202    |   0.0495 |             3.889 | deadband_floored;high_false_alarm                     |
| C6_0        |      4 | settled         | C58         |       6814 |   11.9914    |   0.053271   |   0.0044 |             5.063 | high_false_alarm                                      |
| C6_0        |      4 | transient       | C18         |       6814 |    1.86151   |   1.86984    |   1.0045 |             0.191 | wide_limits;high_cv                                   |
| C6_0        |      5 | settled         | C11         |       2397 |   -1.29839   |   0.486165   |   0.3744 |             0.042 | deadband_floored;wide_limits                          |
| C6_0        |      5 | settled         | C15         |       2397 |   23         |   0          |   0      |             0     | zero_sigma                                            |
| C6_0        |      5 | settled         | C16         |       2397 |  241.49      |   6.44655    |   0.0267 |             0     | deadband_floored                                      |
| C6_0        |      5 | settled         | C31         |       2397 |    0.533066  |   1.63205    |   3.0616 |             5.882 | deadband_floored;high_false_alarm;wide_limits;high_cv |
| C6_0        |      5 | settled         | C32         |       2397 |    0.0422535 |   0.625215   |  14.7968 |             1.752 | deadband_floored;wide_limits;high_cv                  |
| C6_0        |      5 | settled         | C57         |       2397 |   11         |   0          |   0      |             0     | zero_sigma;deadband_floored                           |
| C6_0        |      5 | settled         | C63         |       2397 |  202.342     |   3.26911    |   0.0162 |             0     | deadband_floored                                      |
| C6_0        |      6 | settled         | C11         |       6814 |   -1.212     |   0.372349   |   0.3072 |             0     | deadband_floored;wide_limits                          |
| C6_0        |      6 | settled         | C15         |       6814 |   23         |   0          |   0      |             0     | zero_sigma                                            |
| C6_0        |      6 | settled         | C16         |       6814 |  235         |   0          |   0      |             0     | zero_sigma                                            |
| C6_0        |      6 | settled         | C31         |       6814 |    0         |   0          | nan      |             0     | zero_sigma                                            |
| C6_0        |      6 | settled         | C32         |       6814 |    0         |   0          | nan      |             0     | zero_sigma                                            |
| C6_0        |      6 | settled         | C57         |       6814 |   11         |   0          |   0      |             0     | zero_sigma                                            |
| C6_0        |      6 | settled         | C61         |       6814 | -253.763     |  19.9343     |   0.0786 |             4.403 | high_false_alarm                                      |
| C6_0        |      6 | settled         | C63         |       6814 |  368.6       |  46.4174     |   0.1259 |             0     | deadband_floored                                      |
| C6_0        |      7 | settled         | C11         |       6814 |   -1.099     |   0.262813   |   0.2391 |             0     | deadband_floored;wide_limits                          |
| C6_0        |      7 | settled         | C15         |       6814 |   23         |   0          |   0      |             0     | zero_sigma                                            |
| C6_0        |      7 | settled         | C16         |       6814 |  235         |   0          |   0      |             0     | zero_sigma                                            |
| C6_0        |      7 | settled         | C31         |       6814 |    0         |   0          | nan      |             0     | zero_sigma                                            |
| C6_0        |      7 | settled         | C32         |       6814 |    0         |   0          | nan      |             0     | zero_sigma                                            |
| C6_0        |      7 | settled         | C57         |       6814 |   11         |   0          |   0      |             0     | zero_sigma                                            |
| C6_0        |      7 | settled         | C58         |       6814 |   11.9875    |   0.00489271 |   0.0004 |             0     | deadband_floored                                      |
| C6_0        |      7 | settled         | C61         |       6814 | -247.772     |   4.32093    |   0.0174 |             2.245 | high_false_alarm                                      |
| C6_0        |      7 | settled         | C63         |       6814 |  688.8       |  99.7505     |   0.1448 |             0     | deadband_floored                                      |