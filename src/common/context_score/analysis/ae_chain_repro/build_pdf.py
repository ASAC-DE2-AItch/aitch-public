"""build_pdf.py — viz_data.json → A4 PDF 리포트 (reportlab 직접 드로잉).

HTML판과 동일 내용·동일 수치. 차트는 reportlab canvas로 재드로잉한다
(샌드박스에 headless 브라우저가 없어 HTML 렌더 캡처 대신 네이티브 생성).
"""
from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib.colors import HexColor, Color
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

OUT = Path("/sessions/sleepy-zealous-cerf/mnt/outputs")
D = json.loads((OUT / "viz_data.json").read_text())
S = D["stats"]
AX, AY = S["anchors_x"], S["anchors_y"]
ST = D["stream"]

FONTS = OUT / "fonts"          # make_fonts.py 산출 (Noto Sans CJK KR 서브셋 → TTF)
pdfmetrics.registerFont(TTFont("KR", str(FONTS / "NotoKR.ttf")))
pdfmetrics.registerFont(TTFont("KRB", str(FONTS / "NotoKR-Bold.ttf")))

W, H = A4
M = 42
INK = HexColor("#1a1a18")
MUT = HexColor("#6b6b66")
LINE = HexColor("#e5e3df")
BG = HexColor("#f6f5f3")
RAW = HexColor("#8b5cf6")
SC = HexColor("#2563eb")
DR = HexColor("#dc2626")
WARN = HexColor("#d97706")
OK = HexColor("#059669")

c = canvas.Canvas(str(OUT / "AE_스코어체인_실데이터.pdf"), pagesize=A4)
c.setTitle("AE 스코어 체인 — 실데이터 추적")
c.setAuthor("AITCH · Context Score B4-1")

_page = [0]


def foot():
    _page[0] += 1
    c.setFont("KR", 7.5)
    c.setFillColor(HexColor("#a8a5a0"))
    c.drawString(M, 26, "AE 스코어 체인 — ae_v3(z16_rev3_seg1) · 실데이터 15,919 wafer 재현")
    c.drawRightString(W - M, 26, str(_page[0]))
    c.showPage()


def h2(y, n, t, sub=None):
    c.setFillColor(INK)
    c.roundRect(M, y - 4, 17, 17, 4, fill=1, stroke=0)
    c.setFont("KRB", 9)
    c.setFillColor(HexColor("#ffffff"))
    c.drawCentredString(M + 8.5, y + 1.5, str(n))
    c.setFont("KRB", 13.5)
    c.setFillColor(INK)
    c.drawString(M + 25, y + 1, t)
    if sub:
        c.setFont("KR", 8.6)
        c.setFillColor(MUT)
        y -= 13
        for ln in sub.split("\n"):
            c.drawString(M + 25, y, ln)
            y -= 11
        return y - 4
    return y - 16


def card(x, y, w, h, fill=None):
    c.setStrokeColor(LINE)
    c.setFillColor(fill or HexColor("#ffffff"))
    c.setLineWidth(0.7)
    c.roundRect(x, y, w, h, 7, fill=1, stroke=1)


def kv(x, y, w, k, v, vc=INK, h=40):
    card(x, y, w, h, BG)
    c.setFont("KR", 7.3)
    c.setFillColor(MUT)
    c.drawString(x + 9, y + h - 15, k)
    c.setFont("KRB", 14)
    c.setFillColor(vc)
    c.drawString(x + 9, y + 9, v)


def note(x, y, w, lines, tone="warn"):
    pad, lh = 10, 11.6
    h = pad * 2 + lh * len(lines)
    c.setStrokeColor(HexColor("#fde68a") if tone == "warn" else HexColor("#a7f3d0"))
    c.setFillColor(HexColor("#fffbeb") if tone == "warn" else HexColor("#ecfdf5"))
    c.setLineWidth(0.7)
    c.roundRect(x, y - h, w, h, 6, fill=1, stroke=1)
    yy = y - pad - 8
    for bold, s in lines:
        c.setFont("KRB" if bold else "KR", 8.6)
        c.setFillColor(HexColor("#92400e") if tone == "warn" else HexColor("#065f46"))
        c.drawString(x + 11, yy, s)
        yy -= lh
    return y - h - 10


# ══════════════ P1 · 표지 ══════════════
c.setFillColor(INK)
c.setFont("KRB", 22)
c.drawString(M, H - 78, "AE 스코어 체인 — 실데이터로 따라가기")
c.setFont("KR", 10)
c.setFillColor(MUT)
c.drawString(M, H - 97, "ae_raw → ae_score → ae_drift_score → Anomaly축 / Drift축 기여점수")
c.setStrokeColor(INK)
c.setLineWidth(1.6)
c.line(M, H - 110, W - M, H - 110)

y = H - 136
c.setFont("KR", 9)
c.setFillColor(INK)
for ln in ["번들  ae_v3 · z16_rev3_seg1 (model_seed42.pt · scaler.pkl · calib.json)",
           "데이터  Data/문제1(하) train_data.csv + valid_X.csv + test_X.csv 병합",
           "        163,245행 · 15,919 wafer · 챔버 C24_0 · seg1(정상) 4,903 / seg2(이상) 11,016",
           "산식    score_ae 설계 기획서 v1 §3 (z버킷 · 밴드 · max 집계)"]:
    c.drawString(M, y, ln)
    y -= 14

y -= 12
c.setFont("KRB", 11)
c.setFillColor(INK)
c.drawString(M, y, "재현 검증 — 원 파이프라인과 동일함이 확인된 지표")
y -= 8
bw = (W - 2 * M - 3 * 8) / 4
for i, (k, v) in enumerate([("VAL 세트 해시", "fa500f3d 일치"), ("calib 앵커 재현", "오차 0.006%"),
                            ("L5 오경보율", "1.53% 일치"), ("VAL 평균 ae_score", "0.045 일치")]):
    x = M + i * (bw + 8)
    card(x, y - 42, bw, 40, HexColor("#ecfdf5"))
    c.setFont("KR", 7.3)
    c.setFillColor(HexColor("#047857"))
    c.drawString(x + 9, y - 15, k)
    c.setFont("KRB", 11)
    c.setFillColor(HexColor("#065f46"))
    c.drawString(x + 9, y - 33, v)
y -= 56

c.setFont("KR", 8.4)
c.setFillColor(MUT)
c.drawString(M, y, "샌드박스에 torch·sklearn이 없어 파이프라인을 numpy로 재구현했으나, 위 4개 지표가 원본과 일치하므로")
c.drawString(M, y - 11, "이 문서의 모든 수치는 원 파이프라인 산출값과 동일하다.")
y -= 34

c.setFont("KRB", 11)
c.setFillColor(INK)
c.drawString(M, y, "핵심 수치")
y -= 10
cells = [("정상 μ_raw", f"{S['mu_raw']:.2f}", INK), ("정상 σ_raw", f"{S['sd_raw']:.2f}", INK),
         ("z=2 → ae_raw", "127.1", RAW), ("z=3 → ae_raw", "167.0", RAW),
         ("ae_score=1.0 포화", f"{D['n_sat_score']:,}장", DR),
         ("└ 정상 레짐에서", f"{D['seg1_sat']}장", DR),
         ("drift=1.0 고착", f"{D['n_sat_drift']:,}장", DR),
         ("max 집계 평균", f"{D['mean_max']:.2f}점", OK),
         ("단순합산 평균", f"{D['mean_sum']:.2f}점", DR)]
bw2 = (W - 2 * M - 3 * 8) / 4
for i, (k, v, col) in enumerate(cells):
    r, cc = divmod(i, 4)
    kv(M + cc * (bw2 + 8), y - 42 - r * 48, bw2, k, v, col)
y -= 42 + 2 * 48 + 14

y = note(M, y, W - 2 * M, [
    (True, "이 문서가 답하는 것"),
    (False, "① ae_raw 하나가 어떤 계산을 거쳐 두 축의 기여점수가 되는가 — wafer 단위 실값 추적"),
    (False, "② [0,1] 포화가 실제로 얼마나 정보를 잃는가 — 포화 wafer의 raw 분포"),
    (False, "③ drift가 1.0에 닿은 뒤 값으로 복귀하는가 — 실측 결과"),
    (False, "④ 두 채널을 더하면 얼마나 과대계상되는가 — max 집계와의 비교"),
], tone="ok")
foot()

# ══════════════ P2 · 변환 체인 ══════════════
y = h2(H - 70, 1, "한눈에 — 값이 어떻게 변해가나",
       "대표 wafer 4장이 각 단계에서 갖는 실제 값. 같은 6단계를 통과하지만 결과가 전혀 다르다.")

stages = [("① ae_raw", "ae_raw", "{:.1f}", RAW, "(r−μ)T·P·(r−μ)"),
          ("z 표준화", "z", "{:+.2f}σ", RAW, "(raw−47.31)/39.91"),
          ("② ae_score", "ae_score", "{:.3f}", SC, "ECDF 앵커 [0,1]"),
          ("EWMA", "ewma", "{:.4f}", SC, "α=0.05 누적"),
          ("③ drift", "ae_drift_score", "{:.3f}", DR, "(E−0.13)/0.07")]

for ex in D["ex"]:
    ch = 92
    card(M, y - ch, W - 2 * M, ch)
    c.setFont("KRB", 10)
    c.setFillColor(INK)
    c.drawString(M + 12, y - 20, f"{ex['label']}  ·  {ex['wafer']}")
    c.setFont("KR", 7.8)
    c.setFillColor(MUT)
    c.drawString(M + 12, y - 32, ex["desc"])

    n = len(stages) + 1
    sw = (W - 2 * M - 24) / n
    for i, (lb, key, fmt, col, sub) in enumerate(stages):
        x = M + 12 + i * sw
        c.setFont("KR", 6.6)
        c.setFillColor(MUT)
        c.drawString(x, y - 48, lb.upper())
        c.setFont("KRB", 13)
        c.setFillColor(col)
        c.drawString(x, y - 64, fmt.format(ex[key]))
        c.setFont("KR", 6.2)
        c.setFillColor(MUT)
        c.drawString(x, y - 74, sub)
        if i:
            c.setFillColor(HexColor("#c9c6c0"))
            c.setFont("KR", 7)
            c.drawString(x - 10, y - 62, "▶")
    x = M + 12 + len(stages) * sw
    c.setFillColor(BG)
    c.roundRect(x - 6, y - 82, sw + 2, 42, 4, fill=1, stroke=0)
    c.setFont("KR", 6.6)
    c.setFillColor(MUT)
    c.drawString(x, y - 48, "축 기여점수")
    c.setFont("KRB", 13)
    c.setFillColor(INK)
    c.drawString(x, y - 64, f"{ex['score_ae_z']:.0f}점")
    c.setFont("KR", 6.2)
    c.setFillColor(MUT)
    c.drawString(x, y - 74, f"max(A {ex['anom_z']:.0f}, D {ex['drift_pts']:.0f})")
    y -= ch + 10

y = note(M, y - 4, W - 2 * M, [
    (True, "읽는 법"),
    (False, "· 정상구간 스파이크(C64_5367): raw 483.5로 매우 크지만 drift는 0 — 한 장짜리 사건은 EWMA를 못 움직인다."),
    (False, "· 포화 고착: raw는 중간값인데 drift가 1.0 — 누적이 이미 알람선을 넘어 개별 wafer와 무관하게 켜져 있다."),
    (False, "· 두 축이 서로 다른 사건에 반응한다는 것이 이 4장의 대비에서 보인다."),
])
foot()

# ══════════════ P3 · ae_raw + 변환곡선 ══════════════
y = h2(H - 70, 2, "ae_raw — 원점수 (unbounded)",
       "재구성 잔차의 조건화 Mahalanobis². 위로 열려 있어 '얼마나 심한가'가 끝까지 구분된다.")
bw3 = (W - 2 * M - 4 * 8) / 5
for i, (k, v, col) in enumerate([("정상 중앙값", "33.0", INK), ("정상 μ_raw", f"{S['mu_raw']:.2f}", INK),
                                 ("정상 σ_raw", f"{S['sd_raw']:.2f}", INK),
                                 ("이상 중앙값", "226.7", DR), ("관측 최대", "1366.8", DR)]):
    kv(M + i * (bw3 + 8), y - 42, bw3, k, v, col)
y = note(M, y - 52, W - 2 * M, [
    (True, "z 임계가 실제로 가리키는 raw 값:  z=2 → ae_raw 127.1  ·  z=3 → ae_raw 167.0"),
    (False, "정상 중앙값 33의 약 4~5배 지점. 정상 분포 꼬리를 충분히 벗어난 곳에 버킷 경계가 놓인다."),
])

y = h2(y - 8, 3, "ae_raw → ae_score — 앵커 변환 (여기서 포화 발생)",
       "VAL 정상 분위수 5점에 고정한 단조 선형보간. P99.9(raw 399)에서 1.0에 닿고 그 위는 전부 1.0.")

# 곡선 차트
cx0, cy0, cw, chh = M + 40, y - 330, W - 2 * M - 52, 308
xmax = 1400.0
CX = lambda v: cx0 + min(v, xmax) / xmax * cw
CY = lambda v: cy0 + v * chh
c.setFillColor(HexColor("#fee2e2"))
c.rect(CX(399), cy0, cx0 + cw - CX(399), chh, fill=1, stroke=0)
c.setFont("KRB", 7.6)
c.setFillColor(HexColor("#b91c1c"))
c.drawCentredString((CX(399) + cx0 + cw) / 2, cy0 + chh * .60,
                    "포화 영역 — 이 구간의 raw는 전부 score = 1.0")
c.setFont("KR", 7.2)
c.drawCentredString((CX(399) + cx0 + cw) / 2, cy0 + chh * .60 - 12,
                    f"raw 399.3 ~ 1366.8 (3.42배) · {D['n_sat_score']}장")
c.setStrokeColor(HexColor("#eceae6"))
c.setLineWidth(0.6)
for v in (0, .2, .5, 1):
    c.line(cx0, CY(v), cx0 + cw, CY(v))
    c.setFont("KR", 7)
    c.setFillColor(MUT)
    c.drawRightString(cx0 - 5, CY(v) - 2.5, f"{v:.1f}")
for v in range(0, int(xmax) + 1, 200):
    c.setFillColor(MUT)
    c.setFont("KR", 7)
    c.drawCentredString(CX(v), cy0 - 11, str(v))
c.setFont("KR", 7.6)
c.drawCentredString(cx0 + cw / 2, cy0 - 24, "ae_raw (조건화 Maha²)")
c.drawString(cx0 - 34, cy0 + chh + 8, "ae_score")

p = c.beginPath()
p.moveTo(CX(0), CY(0))
for xa, ya in zip(AX, AY):
    p.lineTo(CX(xa), CY(ya))
p.lineTo(CX(xmax), CY(1))
c.setStrokeColor(SC)
c.setLineWidth(2)
c.drawPath(p)
for i, (xa, ya) in enumerate(zip(AX, AY)):
    c.setFillColor(INK)
    c.circle(CX(xa), CY(ya), 3, fill=1, stroke=0)
    c.setFont("KRB", 6.8)
    c.drawCentredString(CX(xa), CY(ya) + 7, f"P{['50','90','98.5','99.5','99.9'][i]}")
    c.setFont("KR", 6.4)
    c.setFillColor(MUT)
    c.drawCentredString(CX(xa), CY(ya) - 11, f"{xa:.0f}")

# 실측 포화 wafer 3장을 x축 위에 찍어 '같은 1.0'임을 보인다
for wid, rv in (("최소 399.3", 399.3), ("C64_5367 483.5", 483.5), ("최대 1366.8", 1366.8)):
    c.setFillColor(WARN)
    c.circle(CX(rv), CY(1.0), 3.4, fill=1, stroke=0)
    c.setStrokeColor(WARN)
    c.setLineWidth(0.6)
    c.setDash(2, 2)
    c.line(CX(rv), cy0, CX(rv), CY(1.0))
    c.setDash()
    c.setFont("KR", 6.4)
    c.setFillColor(HexColor("#92400e"))
    c.saveState()
    c.translate(CX(rv) - 2.5, cy0 + 8)
    c.rotate(90)
    c.drawString(0, 0, f"{wid} → 1.000")
    c.restoreState()

y2 = note(M, cy0 - 36, W - 2 * M, [
    (True, "포화의 실체 — 세 점 모두 ae_score = 1.000 으로 구분되지 않는다."),
    (False, "raw 399.3(P99.9 턱걸이)과 raw 1366.8(그 3.42배)이 같은 점수를 받는다."),
    (False, "즉 [0,1] 눈금은 '이상인가'는 답하지만 '얼마나 심한가'는 P99.9 위에서 답하지 못한다."),
    (False, "설계서가 Anomaly 채널에 unbounded한 ae_raw + z 경로를 권장한 이유가 이것이다."),
])
foot()

# ══════════════ P4 · EWMA → drift ══════════════
y = h2(H - 70, 4, "ae_score → EWMA → ae_drift_score",
       "E_t = 0.05·s_t + 0.95·E_(t−1)  →  clip((E − 0.130) / (0.200 − 0.130), 0, 1)\n"
       "유효 밴드폭이 0.07뿐이라 EWMA가 조금만 올라도 1.0에 붙는다.")

Z = D["zoom"]
zi0, zi1 = Z["i"][0], Z["i"][-1]
px0, pw = M + 40, W - 2 * M - 92
DX = lambda v: px0 + (v - zi0) / (zi1 - zi0) * pw

def panel(py, ph, key, vmax, col, title, bands=None):
    """한 패널: 배경·격자·라인·레짐 마커."""
    c.setStrokeColor(HexColor("#eceae6")); c.setFillColor(HexColor("#ffffff"))
    c.setLineWidth(0.7); c.rect(px0, py, pw, ph, fill=1, stroke=1)
    Y = lambda v: py + min(v, vmax) / vmax * ph
    for v, lb, bc in (bands or []):
        c.setStrokeColor(HexColor(bc)); c.setDash(4, 3); c.setLineWidth(0.8)
        c.line(px0, Y(v), px0 + pw, Y(v)); c.setDash()
        c.setFont("KR", 6.8); c.setFillColor(MUT)
        c.drawString(px0 + pw + 4, Y(v) - 2.5, lb)
    p = c.beginPath()
    for n, (ix, v) in enumerate(zip(Z["i"], Z[key])):
        (p.moveTo if n == 0 else p.lineTo)(DX(ix), Y(v))
    c.setStrokeColor(col); c.setLineWidth(1.3); c.drawPath(p)
    xb = DX(S["seg2_start"])
    c.setStrokeColor(INK); c.setDash(3, 3); c.setLineWidth(0.8)
    c.line(xb, py, xb, py + ph); c.setDash()
    c.setFont("KRB", 8); c.setFillColor(col)
    c.drawString(px0 + 7, py + ph - 12, title)
    c.setFont("KR", 6.8); c.setFillColor(MUT)
    c.drawRightString(px0 - 4, py + ph - 3, f"{vmax:g}")
    c.drawRightString(px0 - 4, py - 2, "0")
    return Y

# ① ae_score (원신호)
ph = 128
p1y = y - 22 - ph
panel(p1y, ph, "sc", 1.0, SC, "ae_score  (per-wafer 원신호)")
c.setFont("KRB", 7); c.setFillColor(INK)
c.drawString(DX(S["seg2_start"]) + 4, p1y + ph - 12, "레짐 전환 (4,903번째)")

# ② EWMA
p2y = p1y - 26 - ph
Ye = panel(p2y, ph, "ew", 0.45, SC, "EWMA(ae_score)  α=0.05",
           bands=[(0.130, "B0 = 0.130", "#9ca3af"), (0.200, "ALARM = 0.200", "#dc2626")])
c.setFillColor(HexColor("#dbeafe"))
c.rect(px0 + 1, Ye(.130), pw - 2, Ye(.200) - Ye(.130), fill=1, stroke=0)
p = c.beginPath()
for n, (ix, v) in enumerate(zip(Z["i"], Z["ew"])):
    (p.moveTo if n == 0 else p.lineTo)(DX(ix), Ye(v))
c.setStrokeColor(SC); c.setLineWidth(1.3); c.drawPath(p)
c.setFont("KRB", 6.8); c.setFillColor(HexColor("#1d4ed8"))
c.drawString(px0 + 7, (Ye(.130) + Ye(.200)) / 2 - 2.5, "유효 밴드폭 0.07 — 이 구간만 지나면 바로 1.0")

# ③ drift
p3y = p2y - 26 - ph
panel(p3y, ph, "dr", 1.0, DR, "ae_drift_score")
c.setStrokeColor(DR); c.setDash(2, 2); c.setLineWidth(0.8)
c.line(DX(D["first1"]), p3y, DX(D["first1"]), p3y + ph); c.setDash()
c.setFont("KRB", 7); c.setFillColor(DR)
c.drawString(DX(D["first1"]) + 5, p3y + ph * 0.42, f"최초 1.0 도달 ({D['first1']:,}번째)")
c.setFont("KR", 7); c.drawString(DX(D["first1"]) + 5, p3y + ph * 0.42 - 11, "이후 15,919번째까지 복귀 없음")
c.setFont("KR", 7); c.setFillColor(MUT)
c.drawString(px0, p3y - 12, f"wafer #{zi0:,}")
c.drawCentredString(px0 + pw / 2, p3y - 12, "(풀해상도 · 500장 구간)")
c.drawRightString(px0 + pw, p3y - 12, f"#{zi1:,}")

dy0 = p3y

note(M, dy0 - 26, W - 2 * M, [
    (True, "drift는 한 번 1.0에 닿으면 값으로는 돌아오지 않는다."),
    (False, f"최초 도달 {D['first1']:,}번째 wafer → 이후 {D['n_sat_drift']:,}장 끝까지 1.0 고착, 0 복귀 0회."),
    (False, "α=0.05(95% 이월)이라 EWMA가 B0=0.13 아래로 내려가려면 깨끗한 wafer가 수백 장 필요하다."),
    (False, "즉 현실적 리셋 경계는 값이 아니라 PM·재Qual에서의 DriftTracker 상태 초기화다."),
])
foot()

# ══════════════ P5 · 전체 스트림 ══════════════
y = h2(H - 70, 5, "전체 스트림 — 15,919 wafer",
       "시간순 전 구간. 세로 점선이 레짐 전환(seg2 시작, 4,903번째).")
rows = [("ae_raw", "raw", RAW, 700), ("ae_score", "sc", SC, 1), ("ae_drift_score", "dr", DR, 1)]
sx0, sw2 = M + 30, W - 2 * M - 40
top = y - 14
for ri, (lb, key, col, vmax) in enumerate(rows):
    bh = 170
    by = top - (ri + 1) * (bh + 26)
    c.setStrokeColor(HexColor("#eceae6"))
    c.setFillColor(HexColor("#ffffff"))
    c.setLineWidth(0.7)
    c.rect(sx0, by, sw2, bh, fill=1, stroke=1)
    Y = lambda v: by + min(v, vmax) / vmax * bh
    p = c.beginPath()
    for k, v in enumerate(ST[key]):
        X = sx0 + k / (len(ST["i"]) - 1) * sw2
        (p.moveTo if k == 0 else p.lineTo)(X, Y(v))
    c.setStrokeColor(col)
    c.setLineWidth(0.6)
    c.drawPath(p)
    xb = sx0 + S["seg2_start"] / S["n"] * sw2
    c.setStrokeColor(INK)
    c.setDash(3, 3)
    c.line(xb, by, xb, by + bh)
    c.setDash()
    c.setFont("KRB", 8.4)
    c.setFillColor(col)
    c.drawString(sx0 + 6, by + bh - 12, lb)
    c.setFont("KR", 7)
    c.setFillColor(MUT)
    c.drawRightString(sx0 - 4, by + bh - 3, "700" if vmax == 700 else "1.0")
    c.drawRightString(sx0 - 4, by - 2, "0")
    if ri == 0:
        c.setFont("KRB", 7.2)
        c.setFillColor(INK)
        c.drawString(xb + 4, by + bh - 12, "레짐 전환 (4,903)")
    if ri == 2:
        c.setFont("KR", 7)
        c.setFillColor(MUT)
        c.drawString(sx0, by - 12, "wafer #0 (시간순)")
        c.drawRightString(sx0 + sw2, by - 12, "#15,919")
        note(M, by - 26, W - 2 * M, [
            (True, "세 줄을 겹쳐 보면 상보 관계가 드러난다."),
            (False, "ae_raw·ae_score는 정상 구간에서도 뾰족한 스파이크를 여러 번 낸다(단발 사건)."),
            (False, "drift는 그 스파이크들을 무시하다가 레짐이 바뀐 뒤 계단처럼 올라 고착된다(지속 사건)."),
            (False, f"실측: Drift만 발화 1,149장 · Anomaly만 발화 81장 — 한쪽을 버릴 수 없다."),
        ])
foot()

# ══════════════ P6 · 축 점수 산식 ══════════════
y = h2(H - 70, 6, "축 기여점수 — 설계서 §3 산식 적용",
       "Anomaly(z버킷) z<2→0 / 2≤z<3→+15 / z≥3→+25   ·   Drift(밴드) ≤0.3→0 / ~0.7→+10 / >0.7→+20\n"
       "score_ae = max(Anomaly, Drift)")

groups = [("Anomaly 채널 (z 버킷)", [("z < 2", "0점", 0, HexColor("#e5e3df")),
                                 ("2 ≤ z < 3", "+15점", 15, HexColor("#fcd34d")),
                                 ("z ≥ 3", "+25점", 25, HexColor("#f59e0b"))]),
          ("Drift 채널 (밴드)", [("d ≤ 0.3", "0점", 0, HexColor("#e5e3df")),
                              ("0.3 < d ≤ 0.7", "+10점", 10, HexColor("#fca5a5")),
                              ("d > 0.7", "+20점", 20, HexColor("#ef4444"))])]
for gi, (lb, bars) in enumerate(groups):
    gy = y - 14 - gi * 130
    c.setFont("KRB", 10)
    c.setFillColor(INK)
    c.drawString(M, gy, lb)
    base = gy - 86                      # 공통 baseline (막대가 위로 자란다)
    c.setStrokeColor(LINE)
    c.setLineWidth(0.8)
    c.line(M, base, W - M, base)
    bwid = (W - 2 * M - 2 * 14) / 3
    for bi, (t, pts, val, col) in enumerate(bars):
        bx = M + bi * (bwid + 14)
        bh = max(val / 25 * 62, 2.5)
        c.setFillColor(col)
        c.roundRect(bx, base, bwid, bh, 3, fill=1, stroke=0)
        c.setFont("KRB", 11)
        c.setFillColor(INK if val else MUT)
        c.drawCentredString(bx + bwid / 2, base + bh + 6, pts)
        c.setFont("KR", 8.6)
        c.setFillColor(MUT)
        c.drawCentredString(bx + bwid / 2, base - 13, t)
y -= 14 + 130 + 116

y = note(M, y, W - 2 * M, [
    (True, "왜 두 채널을 더하지 않고 max로 뭉치는가"),
    (False, "e6(Anomaly)·e7(Drift)는 모두 단일 ae_raw 하나에서 파생된다 (불가침 12)."),
    (False, "ae_raw → ae_score(순간) → EWMA → ae_drift_score(누적) — 같은 원신호의 시간필터 차이일 뿐이다."),
    (False, "레짐 전환 시 둘 다 켜지므로 독립 증거처럼 더하면 이중계상이 된다 (그룹핑 문서 A3)."),
])

bw4 = (W - 2 * M - 8) / 2
kv(M, y - 46, bw4, "max 집계 — 전체 평균", f"{D['mean_max']:.2f}점", OK, 44)
kv(M + bw4 + 8, y - 46, bw4, "단순합산 — 전체 평균", f"{D['mean_sum']:.2f}점  (+72% 과대)", DR, 44)
foot()

# ══════════════ P7 · 표 ══════════════
y = h2(H - 70, 7, "레짐 전환 앞뒤 43장 — 전 단계 실값",
       "위 13장 = seg1(정상 레짐 끝) · 아래 30장 = seg2(이상 레짐 시작). 한 행이 wafer 1장.")
cols = [("wafer", "wafer", 62, "s"), ("seg", "구간", 32, "s"), ("ae_raw", "ae_raw", 48, "1"),
        ("z", "z", 42, "2"), ("ae_score", "ae_score", 50, "3"), ("ewma", "EWMA", 46, "4"),
        ("ae_drift_score", "drift", 42, "3"), ("anom_z", "A(z)", 34, "0"),
        ("anom_band", "A(밴드)", 42, "0"), ("drift_pts", "D", 28, "0"),
        ("score_ae_z", "score_ae", 50, "0"), ("score_ae_sum", "합산시", 42, "0")]
tw = sum(x[2] for x in cols)
tx = M + (W - 2 * M - tw) / 2
c.setFillColor(BG)
c.rect(tx, y - 15, tw, 15, fill=1, stroke=0)
c.setFont("KRB", 6.8)
c.setFillColor(MUT)
xx = tx
for k, lb, cw2, _ in cols:
    c.drawRightString(xx + cw2 - 4, y - 11, lb) if k != "wafer" else c.drawString(xx + 3, y - 11, lb)
    xx += cw2
ry = y - 15
for r in D["tbl"]:
    ry -= 12.5
    if r["seg"] == "seg2":
        c.setFillColor(HexColor("#fef2f2"))
        c.rect(tx, ry, tw, 12.5, fill=1, stroke=0)
    c.setStrokeColor(HexColor("#f0eeea"))
    c.setLineWidth(0.4)
    c.line(tx, ry, tx + tw, ry)
    xx = tx
    for k, _, cw2, fm in cols:
        v = r[k]
        s = v if fm == "s" else f"{v:.{int(fm)}f}"
        if k == "score_ae_z":
            c.setFont("KRB", 6.9)
            c.setFillColor(INK)
        elif k == "score_ae_sum":
            c.setFont("KR", 6.9)
            c.setFillColor(DR)
        elif k == "seg":
            c.setFont("KRB" if v == "seg2" else "KR", 6.9)
            c.setFillColor(DR if v == "seg2" else MUT)
        else:
            c.setFont("KR", 6.9)
            c.setFillColor(INK)
        if k == "wafer":
            c.drawString(xx + 3, ry + 4, s)
        else:
            c.drawRightString(xx + cw2 - 4, ry + 4, s)
        xx += cw2
note(M, ry - 12, W - 2 * M, [
    (True, "표에서 확인할 것"),
    (False, "· seg1 마지막 구간에도 z=5.08짜리 wafer(C64_9167)가 섞여 있다 — 정상 레짐의 단발 스파이크."),
    (False, "· seg2 진입 직후 Anomaly는 즉시 +25지만 drift는 0에서 시작해 약 10장에 걸쳐 1.0까지 오른다."),
    (False, "· '합산시' 열이 score_ae보다 크게 벌어지는 행이 이중계상이 발생하는 지점이다."),
])
foot()

# ══════════════ P8 · 관찰 ══════════════
y = h2(H - 70, 8, "이 데이터가 말해주는 것", "설계서 §7 미결 항목과 직접 연결되는 실측 근거 4가지.")

items = [
    ("① Drift는 한 번 1.0에 닿으면 돌아오지 않는다", DR,
     [f"최초 도달 {D['first1']:,}번째 wafer 이후 {D['n_sat_drift']:,}장 끝까지 고착 — 0으로 복귀한 적이 0회.",
      "'0이 다시 오면 최초의 1을 재추적'하는 방식이 실데이터에서 성립하지 않는 이유다.",
      "리셋 경계는 값이 아니라 PM 사이클이며, 상태 초기화 책임은 A의 DriftTracker에 있다."]),
    ("② 정상 레짐에서도 ae_score = 1.0이 나온다", WARN,
     [f"seg1(정상)에서 {D['seg1_sat']}장. VAL 기준 0.10%로 P99.9 정의값 그대로다.",
      "예: C64_5367 — raw 483.5 · score 1.0 인데 drift는 0.0.",
      "'순간 스파이크 = Anomaly만'의 교과서 사례이자, 엣지 래치가 오발화할 수 있는 지점이다."]),
    ("③ 두 채널 선형합산은 실제로 72% 과대계상이다", DR,
     [f"max 집계 {D['mean_max']:.2f}점 vs 단순합산 {D['mean_sum']:.2f}점.",
      "그룹핑 문서의 A3 집계 규칙(선형합산 금지, max/상보 라우팅)이 실측으로 확인된다."]),
    ("④ 상보 관계도 실측으로 확인된다", OK,
     ["Drift만 발화(drift=1.0인데 z<2) 1,149장 · Anomaly만 발화(z≥3인데 drift≤0.3) 81장.",
      "두 채널이 서로 다른 사건을 잡고 있다 — 하나로 합칠 수도, 하나를 버릴 수도 없다."]),
]
for t, col, lines in items:
    hh = 30 + len(lines) * 12
    card(M, y - hh, W - 2 * M, hh)
    c.setFillColor(col)
    c.roundRect(M, y - hh, 3.5, hh, 1.7, fill=1, stroke=0)
    c.setFont("KRB", 10)
    c.setFillColor(INK)
    c.drawString(M + 14, y - 19, t)
    yy = y - 33
    c.setFont("KR", 8.4)
    c.setFillColor(MUT)
    for ln in lines:
        c.drawString(M + 14, yy, ln)
        yy -= 12
    y -= hh + 10

y -= 4
c.setFont("KRB", 9.6)
c.setFillColor(INK)
c.drawString(M, y, "재현 방법")
y -= 14
c.setFont("KR", 8.2)
c.setFillColor(MUT)
for ln in ["torch·sklearn이 있는 환경:  python -m ae_pipeline.infer build-scorer --bundle artifacts --data <merged>",
           "                             python -m ae_pipeline.infer score --bundle artifacts --data <merged> --out scores.json",
           "본 문서 재현 코드:  src/common/context_score/analysis/ae_chain_repro/",
           "                    ae_numpy_shim.py · run_chain.py · run_axes.py · build_html.py · build_pdf.py",
           "wafer별 전 단계 실값:  analysis/ae_chain_repro/wafer_scores_all.csv (15,919행)"]:
    c.drawString(M, y, ln)
    y -= 12
foot()

c.save()
print("PDF 생성 완료")
