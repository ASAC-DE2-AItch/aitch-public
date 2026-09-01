import type { LiveChamberView } from "../live/useLiveFdc";
import { site } from "../mock/fdc";
import type { Incident } from "../mock/fdc";
import { cellsForChamber, countCells } from "../mock/alarms";

/**
 * 인트로 — 장비 전경 한 장 (S12 · 화면 ID PM 승인 대기)
 *
 * 답하는 질문: **"어느 챔버의 어느 축이 문제인가"**  (그 다음 화면 S1 이 "무슨 일이 일어나고 있나")
 *
 * 동작 — 챔버를 클릭(또는 아래로 휠)하면 헤더 아래 전체가 **아래로 슬라이딩**되며
 * 대시보드가 드러난다. 대시보드 맨 위에서 위로 휠하면 인트로로 돌아온다.
 * 전환은 App 의 `.stage`/`.track` 이 담당하고, 이 컴포넌트는 "어느 챔버"만 올려보낸다.
 * 헤더(AITCH·챔버 칩·검색)는 슬라이딩 트랙 밖에 있어 인트로에서도 그대로 남는다.
 *
 * 표기 방식 — **도면 콜아웃**. 챔버에서 지시선을 빼 라벨 상자를 달고, 그 안에
 * `[챔버] 상태` + **감시 축 3종(C4·C11·C17) 목록**을 적어 위험한 축만 색으로 물들인다.
 * 축을 나열하는 이유: "CH3 심각"만으로는 무엇을 볼지 모르지만 "C4 +3.4σ"면 바로 안다.
 * 위험 축에 남은 엔지니어 코멘트가 있으면 말풍선과 건수를 함께 적는다.
 *
 * 왜 선화인가 — 사진은 명암·채도가 강해 ① 블루프린트 라이트 시스템과 재질이 충돌하고
 * ② 앰버·핑크 상태색이 파묻힌다(배경이 먼저 색 원칙을 깬다). 선화는 배경이 무채색이라
 * **상태색이 유일한 색으로 튄다.**
 *
 * 세계관 — **슬롯 6, 장착 4** (mock/fdc.ts: "Etch 클러스터 1대 = 4PM · 시뮬 스펙 v1.3").
 * 클러스터 툴은 슬롯 수와 장착 수가 다르다. 빈 2칸은 **미장착(EMPTY)** 으로 명시한다.
 *
 * 색 문법 (ISA-101 — styles.css·Header.tsx 에 세 번 선언된 원칙을 이행)
 *   장착 챔버 = 무채색 실선 / 미장착 = 점선 EMPTY
 *   **유채색은 이상에만** — 앰버(≥2σ) · 핑크(≥3σ 또는 위반 진행 중, `--blinkO` 점멸).
 *   가관리(provisional)처럼 이상이 아닌 상태는 색을 쓰지 않고 **글자로만** 적는다.
 *   정상 챔버에도 선을 그리는 이유: "슬롯 6 중 몇 개가 돌고 있나"가 개수로 읽혀야 한다
 *   (이상만 그리면 4장착이 2로 보인다 — 실사용 피드백). 무채색 선은 색 신호가 아니다.
 */

/**
 * 챔버 **뚜껑** 외곽선 — 원본 이미지 픽셀 좌표(ART_W × ART_H).
 * 화면에 그리는 것은 이 뚜껑을 아래로 밀어 만든 **몸통 실루엣**이다(`DEPTH_RATIO`·`extrude`).
 *
 * ⚠️ 손으로 찍은 값이 아니다. 같은 해상도의 **빨간 표시선 렌더 이미지**에서
 * `tools/extract_chambers.py` 가 추출한 결과다. 이미지를 교체하면 그 스크립트를 다시 돌린다.
 * 모서리가 각져 보이면 점 수를 늘린다 — `--pts 28` (기본 14).
 * 순서 = 좌상단부터 시계방향(= 물리 슬롯 순서). 어느 슬롯이 장착인지는 `MOUNTED`.
 *
 * **예외는 슬롯 5 하나** — 스크립트가 그 챔버만 실측이 아니라 평균 템플릿을 얹기 때문에
 * 선화에서 다시 적합했다. 사유·방법은 그 항목의 주석에 적어 뒀다.
 */
type Hotspot = {
  pts: [number, number][];
  cx: number;
  cy: number;
  /** 이 챔버만 다른 몸통 깊이 (미지정 = `DEPTH_RATIO`) — 아래 슬롯 5 주석 참조 */
  depthRatio?: number;
};

const HOTSPOTS: Hotspot[] = [
  { cx: 705, cy: 284, pts: [[524, 284], [532, 269], [671, 187], [700, 176], [743, 174], [882, 243], [907, 264], [900, 288], [811, 344], [743, 382], [712, 388], [676, 384], [547, 310], [524, 286]] },
  { cx: 981, cy: 120, pts: [[804, 120], [821, 102], [921, 45], [965, 25], [1030, 27], [1142, 83], [1168, 99], [1166, 127], [1147, 140], [1022, 212], [985, 218], [944, 211], [818, 143], [804, 127]] },
  { cx: 1398, cy: 99, pts: [[1246, 98], [1257, 88], [1380, 16], [1421, 9], [1449, 12], [1474, 21], [1603, 87], [1610, 106], [1494, 186], [1461, 201], [1412, 202], [1274, 136], [1249, 121], [1244, 102]] },
  { cx: 1690, cy: 252, pts: [[1515, 251], [1526, 231], [1574, 201], [1658, 153], [1717, 150], [1871, 222], [1890, 236], [1892, 256], [1878, 270], [1744, 352], [1694, 352], [1660, 338], [1528, 268], [1516, 254]] },
  // 슬롯 4(= CH3, 우측) — 뚜껑 좌표는 추출 그대로. **깊이만** 키웠다 (2026-08-12 요청).
  // 이 챔버는 우측 앞줄이라 가림이 적어 몸통이 거의 다 보이는데, `DEPTH_RATIO` 0.48 은
  // 가림이 큰 뒤쪽 기준이라 테두리가 **몸통 중턱(y 850)에서 끊겼다** — 실측 챔버 밑동은 968.
  // 실측 높이 560 은 앞쪽 슬롯 5(581)와 거의 같다: 같은 챔버이므로 이쪽이 맞는 값이다.
  // ⚠ 늘린 만큼 테두리 **좌하단이 앞 챔버(슬롯 5) 위를 지난다** — 등각에서 CH3 아래쪽을
  //   그 챔버가 가리기 때문이고, extrude 모델은 볼록 도형이라 그 파인 자리를 따라갈 수 없다.
  //   겹침이 거슬리면 0.62~0.66 으로 낮춰 앞 챔버에 닿기 전에 끊는다(대신 밑동이 다시 짧아진다).
  { cx: 1745, cy: 526, depthRatio: 0.78, pts: [[1542, 526], [1653, 442], [1700, 415], [1743, 411], [1777, 419], [1926, 499], [1942, 515], [1942, 538], [1893, 578], [1781, 648], [1734, 648], [1703, 636], [1558, 555], [1542, 534]] },
  // 슬롯 5(= CH4, 앞쪽) — **선화에서 직접 재적합** (2026-08-12). 나머지 5개와 출처가 다르다.
  //
  // 왜 이 하나만 다른가: `extract_chambers.py` 는 앞쪽 챔버를 가리는 이웃이 없어 **몸통까지
  // 채워지고**, 그래서 뚜껑을 오려내지 못한다. 스크립트는 그 자리에 다른 챔버들의 **평균 뚜껑
  // 템플릿**을 폭·왼쪽꼭짓점에 맞춰 얹는다(스크립트 로그가 그렇게 말한다). 그 템플릿이 실제보다
  // 낮게 놓였고, 깊이(`DEPTH_RATIO`)까지 뒤쪽 챔버 기준이라 **테두리 위아래가 짧게** 그려졌다
  // (실측 상단 59 · 하단 83 부족 — 좌우는 맞았다).
  //
  // 새 값의 출처: 선화의 검은 선을 경계로 이 챔버 내부를 채워(앞쪽이라 외곽선이 닫혀 있다)
  // **실루엣을 실측**한 뒤 — x 1261..1685 · y 589..1170 — 뚜껑을 **균일 배율(0.985)로만**
  // 줄여 위로 56 올리고, 모자란 세로는 전부 **깊이**로 냈다(비율 0.852 = 깊이 352).
  // 최종 테두리 x 1256..1689 · y 585..1175 — 사방 4~5px 바깥(INFLATE 의 의도).
  //
  // ⚠ **세로만 늘리지 말 것.** 첫 시도는 세로 배율을 따로 줘(1.66×) 실루엣은 잘 맞췄지만
  //   뚜껑의 등각 기울기가 무너져 이 챔버만 각도가 달라 보였다 — 좌상변 실측
  //   **다른 슬롯 26.6~30.5° / 세로 확대판 35.4°**. 여섯은 같은 투영의 같은 입체라 각도는
  //   공유해야 한다. 깊이는 수직 이동이라 윗·아랫사슬 기울기를 안 건드리는 반면, 세로 배율은
  //   모든 사변을 세운다. **세로가 모자라면 깊이로 낸다** — 배율은 균일하게만.
  // ⚠ 이 pts 는 "뚜껑 상판"이 아니라 **실루엣에 맞춰 되돌린 도형**이다 — 이미지를 바꿔
  //   `extract_chambers.py` 를 다시 돌리면 이 챔버는 또 템플릿으로 덮이니 재적합이 필요하다.
  { cx: 1470, cy: 701, depthRatio: 0.852, pts: [[1266, 703], [1307, 668], [1372, 630], [1426, 600], [1512, 598], [1549, 614], [1645, 664], [1679, 702], [1649, 731], [1563, 786], [1512, 811], [1432, 806], [1360, 769], [1303, 738]] },
];
const ART_W = 2142;
const ART_H = 2016;

/**
 * 뚜껑 → **몸통까지** 늘리는 깊이 (뚜껑 폭에 대한 비율).
 * 챔버는 세로 원통이라 몸통 실루엣 = 뚜껑을 아래로 밀어 만든 궤적이다. 뚜껑 다각형만
 * 있으면 실루엣이 정확히 계산되므로(아래 `extrude`) 몸통을 따로 딸 필요가 없다.
 * 값 0.48 은 빨간 표시선 렌더에서 실측 — 챔버 폭 421 · 전체 높이 430 · 뚜껑 높이 228 → (430−228)/421.
 *
 * ⚠ 이 값은 **뒤쪽·측면 챔버 기준**이다. 그 챔버들은 몸통 아래쪽이 장비 상판에 가려 덜 보인다.
 * 가림이 없는 앞쪽 챔버는 몸통이 통째로 드러나 더 깊다 — 슬롯별 `depthRatio` 로 덮는다.
 */
const DEPTH_RATIO = 0.48;

/**
 * 외곽선을 챔버보다 이만큼 부풀린다.
 * 선을 실물 edge 에 딱 맞추면 ① 추출 오차 몇 px 이 '삐뚤어짐'으로 보이고
 * ② 다각형 꼭짓점이 둥근 모서리를 잘라 각져 보인다. 강조 링은 대상보다 **살짝 밖에**
 * 있어야 강조로 읽힌다(포커스 링과 같은 이유).
 */
const INFLATE = 1.05;

/** 꼭짓점 둥글리기 반경(원본 좌표 단위) — 14점 다각형의 각진 티를 없앤다 */
const CORNER_R = 34;

/* 구 미니 관리도 상수(GAUGE_W·GAUGE_MAX·LIMIT_SIG·gaugeX) 제거 (2026-08-08) —
   콜아웃이 감시 축 σ 게이지에서 채널 건수로 바뀌면서 쓰는 데가 없어졌다. */
/** 콜아웃 상자 — 표제 1줄 + 건수 2칸 */
const BOX_W = 700;
// 340 → 308 (2026-08-08): 축 3줄(58×3)이 건수 2칸 한 줄로 줄었고, 캡션을 걷은 만큼 칸을
// 위로 올리며 키웠다(112 → 144 — 라벨과 숫자 사이를 벌리느라 한 번 더). 칸이 boxY+278 에서
// 끝나 아래 30 이 남고, 그게 위쪽 여백과 얼추 같다.
const BOX_H = 308;
/** 상자를 놓을 여백 — viewBox 를 그림 밖으로 넓혀 만든다 */
const PAD_L = 760;
const PAD_R = 830;
/** 상자를 viewBox 가장자리에서 띄우는 여백 — 0 이면 테두리 선이 반쪽 잘린다 */
const EDGE = 18;
const PAD_T = 40;
const PAD_B = 60;

/**
 * 어느 슬롯에 챔버가 장착돼 있고, 그 콜아웃을 어디에 두는지 (`views[n]` → `MOUNTED[n]`).
 * 슬롯 6 · 장착 4 라서 2칸이 빈다. **앞쪽·측면을 채우고 뒤쪽 2칸을 비운 이유**:
 * 전경에서 가장 크게 보이는 앞 슬롯이 EMPTY 점선이면 시선이 '없는 것'에 먼저 간다.
 * (실물 슬롯 배치가 확정되면 이 배열만 고친다)
 */
const MOUNTED: { slot: number; side: "L" | "R"; boxY: number; dx?: number }[] = [
  // ⚠ 좌측 상자는 boxY 를 충분히 내려야 한다. 좌상단 정보 패널(.introhead)이 **absolute·z-index 2**
  //   라 SVG 위에 그려지고, 둘 다 화면 왼쪽 위라 겹치면 콜아웃이 통째로 가려진다.
  //   창이 넓으면 SVG 가 height-constrained 로 바뀌며 배율이 커져 상자가 위로 올라붙는다 —
  //   그 최악 배율에서도 패널(현 4줄 ≈ 200px) 아래에 남는 하한이 320. 위쪽 여백은
  //   .introart 의 padding-top 이 함께 확보한다(그림도 그만큼 작아진다).
  //   dx = side 기본 x 에서의 가로 보정(+ 가 그림 쪽). 상자는 .cobox 가 94% 불투명이라
  //   그림에 조금 걸쳐도 내용은 가려지지 않는다.
  // 좌측 상자의 높이 제약은 없어졌다 — .introart 가 padding-left 로 정보 패널 몫을 떼어 둬
  // SVG 자체가 패널 오른쪽에서 시작한다(styles.css .introart 주석). 세로는 자유.
  // dx 는 "그림 빈 영역에 살짝 걸치는" 정도로 되돌렸다 (최대치 550 = 챔버에 닿기 직전).
  { slot: 0, side: "L", boxY: 60, dx: 260 },
  { slot: 3, side: "R", boxY: 30 },
  { slot: 4, side: "R", boxY: 610 },
  { slot: 5, side: "R", boxY: 1190 },
];

/**
 * 뚜껑 다각형을 아래로 밀어 **몸통 실루엣**을 만든다.
 *
 * 볼록한 뚜껑이라면 실루엣은 정확히 "위쪽 사슬 + 아래쪽 사슬을 깊이만큼 내린 것"이다
 * (좌·우 최외곽 꼭짓점이 이음새 = 몸통의 수직 변). 볼록껍질 계산이 필요 없다.
 * 점 순서는 시계방향(추출 스크립트가 보장) → 왼쪽 꼭짓점에서 시계방향이 위쪽 사슬.
 */
function extrude(pts: [number, number][], depth: number): [number, number][] {
  let li = 0;
  let ri = 0;
  pts.forEach((p, i) => {
    if (p[0] < pts[li][0]) li = i;
    if (p[0] > pts[ri][0]) ri = i;
  });
  const chain = (from: number, to: number) => {
    const out: [number, number][] = [];
    for (let i = from; ; i = (i + 1) % pts.length) {
      out.push(pts[i]);
      if (i === to) break;
    }
    return out;
  };
  const upper = chain(li, ri);                                                     // 뚜껑 윗변
  const lower = chain(ri, li).map(([x, y]) => [x, y + depth] as [number, number]); // 밑면
  return [...upper, ...lower];
}

/** 중심에서 균일 확대 — 부풀리기(INFLATE) */
function inflate(pts: [number, number][], k: number): [number, number][] {
  const cx = pts.reduce((a, p) => a + p[0], 0) / pts.length;
  const cy = pts.reduce((a, p) => a + p[1], 0) / pts.length;
  return pts.map(([x, y]) => [cx + (x - cx) * k, cy + (y - cy) * k] as [number, number]);
}

/**
 * 다각형을 **꼭짓점이 둥근 경로**로. 각 꼭짓점에서 두 변을 r 만큼 잘라내고 그 사이를
 * 2차 베지어로 잇는다 (변이 2r 보다 짧으면 그 변 길이에 맞춰 줄인다 — 겹침 방지).
 */
function roundedPath(pts: [number, number][], r: number): string {
  const n = pts.length;
  const cut = (a: [number, number], b: [number, number]) => {
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const len = Math.hypot(dx, dy) || 1;
    const t = Math.min(r, len / 2) / len;
    return [a[0] + dx * t, a[1] + dy * t];
  };
  return pts
    .map((p, i) => {
      const from = cut(p, pts[(i - 1 + n) % n]);
      const to = cut(p, pts[(i + 1) % n]);
      const f = (v: number) => v.toFixed(1);
      return `${i === 0 ? "M" : "L"}${f(from[0])},${f(from[1])} Q${f(p[0])},${f(p[1])} ${f(to[0])},${f(to[1])}`;
    })
    .join(" ") + " Z";
}

/** 챔버별 기하 — 모듈 로드 시 한 번만 계산 */
const GEO = HOTSPOTS.map((h) => {
  const xs = h.pts.map((p) => p[0]);
  const depth = (Math.max(...xs) - Math.min(...xs)) * (h.depthRatio ?? DEPTH_RATIO);
  const body = inflate(extrude(h.pts, depth), INFLATE);
  const bx = body.map((p) => p[0]);
  const by = body.map((p) => p[1]);
  const midY = (Math.min(...by) + Math.max(...by)) / 2;
  return {
    path: roundedPath(body, CORNER_R),
    /** 지시선이 붙는 자리 — 몸통 좌·우 측면의 중간 높이 */
    left: [Math.min(...bx), midY] as [number, number],
    right: [Math.max(...bx), midY] as [number, number],
    cx: h.cx,
    cy: midY,
  };
});

/**
 * 선화 경로 — `public/` 의 정적 URL이다. `import` 가 아닌 이유:
 * import 는 파일이 없으면 **빌드가 죽어** 화면 전체를 못 본다. public URL 은 없으면
 * 그 자리만 비고 나머지는 정상 동작한다 → 이미지 교체가 재빌드 없이 파일 복사만으로 끝난다.
 * 대신 "조용히 비어 보이는" 실패가 되므로 onError 로 안내를 띄운다(아래 artErr).
 */
const TOOL_ART = "/tool-lineart.png";

/** 코멘트 말풍선 — 46×44 기준 경로. transform 으로 자리·크기를 준다 */
/**
 * 결정 대기 표식 — **체크**. 사이드바 「Approvals」 아이콘(`M4 12l5 5 11-11`)과 같은 획이라
 * 도착지가 어디인지 모양만으로 읽힌다.
 * 구 말풍선(💬)은 "코멘트 N건"을 세던 시절의 모양이다 (2026-08-08 교체) — 세는 값이
 * 사람이 남긴 말에서 **결정 대기 건수**로 바뀌었으니 모양도 따라가야 한다. 말풍선을 두면
 * 있지도 않은 코멘트 스레드를 계속 가리킨다.
 */
const CHECK = "M0,16 l12,12 l24,-26";

/* 구 `axisSev`(감시 축 하나의 σ 위험도) 제거 (2026-08-08) — 콜아웃이 채널 건수로 바뀌면서
   쓰는 데가 없어졌다. **판정은 이제 `cellsForChamber` 한 곳**이라 두 번째 판정 코드도 사라진다. */

/**
 * 상태 한 단어 — **좌상단 집계표 전용**.
 * 콜아웃에서는 상태를 챔버 이름의 글자 배경(색)으로 주므로 글자를 겹쳐 적지 않는다.
 * 인트로는 정상인지 아닌지까지만 답한다 — σ·공학값은 다음 화면(S1·S2)의 몫이다.
 */
const SEV_WORD: Record<string, string> = {
  normal: "정상", warning: "주의", critical: "이상", provisional: "가관리", pm: "정비",
};

export default function ToolIntro({
  views, incidents, selectedId, onEnter, onOpenIncident, artErr, onArtErr,
}: {
  views: LiveChamberView[];
  incidents: Incident[];
  selectedId: string;
  /** 챔버 확정 → 대시보드로 슬라이딩 (App 이 처리) */
  onEnter: (id: string) => void;
  /**
   * 코멘트 말풍선 클릭 → 그 코멘트가 달린 **Incident** 로 (승인 체인).
   * 말풍선이 세는 건 챔버 상태가 아니라 **사람이 남긴 말**이고, 그 말은 Incident 에 붙는다 —
   * 그래서 콜아웃 몸통(`onEnter` → 챔버 상태)과 도착지가 다르다.
   */
  onOpenIncident: (incidentId: string) => void;
  artErr: boolean;
  onArtErr: (v: boolean) => void;
}) {
  /**
   * 상태 집계 — 심각도 내림차순으로 한 줄씩.
   * 챔버 번호는 **조치가 필요한 등급에만** 붙인다. 정상까지 번호를 달면 목록이 길어져
   * 정작 봐야 할 줄이 묻힌다(어차피 나머지가 정상이라는 건 뺄셈으로 안다).
   */
  const roll = (["critical", "warning", "provisional", "pm", "normal"] as const)
    .map((sev) => ({
      sev,
      label: SEV_WORD[sev],
      ids: views.filter((v) => v.ch.sev === sev).map((v) => v.ch.id),
      withIds: sev === "critical" || sev === "warning",
    }))
    .filter((r) => r.ids.length > 0);

  const viewBox = `${-PAD_L} ${-PAD_T} ${PAD_L + ART_W + PAD_R} ${PAD_T + ART_H + PAD_B}`;

  return (
    <div className="intro">
      {/* 좌상단 표제 — 헤더가 이미 AITCH·챔버 칩을 들고 있으므로 여기서는 **위치와 상태**만 */}
      <div className="introhead">
        {/* 계층 경로 — 팹이 커져도 화면을 다시 짜지 않도록 회사›팹›라인›베이까지 명시 */}
        <nav className="introbc" aria-label="사업장 경로">
          {[site.org, site.fab, site.line, site.bay].map((seg, i) => (
            <span key={seg}>{i > 0 && <i>›</i>}{seg}</span>
          ))}
        </nav>
        <div className="introtool">{views[0]?.ch.tool ?? site.tool}</div>
        <div className="introsub">Etch 클러스터 · 슬롯 {HOTSPOTS.length} · 장착 {views.length}</div>

        {/* 상태 집계 — 색만으로는 '몇 개인지'를 못 세므로 숫자로도 준다 */}
        <ul className="introroll">
          {roll.map((r) => (
            <li key={r.sev} className={"irow " + r.sev}>
              <span className="idot" />
              <span className="iname">{r.label}</span>
              <span className="icount num">{r.ids.length}</span>
              {r.withIds && <span className="iids num">{r.ids.join(" · ")}</span>}
            </li>
          ))}
        </ul>
      </div>

      {/* 선화와 콜아웃을 **한 SVG 좌표계**에 둔다. img 태그를 따로 쓰면 letterbox 여백 때문에
          오버레이 정렬이 어긋날 위험이 있고, viewBox 를 그림 밖으로 넓혀 라벨 자리를 만들 수도 없다. */}
      {/* 매트릭스는 **「종합 현황」으로 옮겼다** (2026-08-07) — 거기가 가로가 넉넉해
          챔버=행·채널=열 방향으로 펴지고, 인트로는 "어느 장비인가"만 답하면 된다.
          여기 세워 두면 첫 화면이 선화와 격자 두 가지를 동시에 읽으라고 요구했다. */}
      <div className={"introart" + (artErr ? " noart" : "")}>
        <svg viewBox={viewBox} role="group" aria-label="장비 전경 — 챔버 선택">
          <image
            href={TOOL_ART} x={0} y={0} width={ART_W} height={ART_H}
            onError={() => onArtErr(true)} onLoad={() => onArtErr(false)}
          />

          {/* 미장착 슬롯 — 점선. 장착 4 / 슬롯 6 이 **개수로 읽혀야** 하므로 비워두지 않는다 */}
          {GEO.map((g, i) =>
            MOUNTED.some((m) => m.slot === i) ? null : (
              <g key={`empty-${i}`} className="hs empty" aria-hidden="true">
                <path d={g.path} className="hsfill" />
                <path d={g.path} className="hsring" />
                {/* 선화와 겹쳐 글자가 묻히므로 판을 깐다. 소문자 괄호 = 강조가 아니라 주석 */}
                <rect x={g.cx - 125} y={g.cy - 40} width={250} height={72} rx={12} className="hstxbg" />
                <text x={g.cx} y={g.cy + 12} className="hstx">(empty)</text>
              </g>
            ),
          )}

          {MOUNTED.map(({ slot, side, boxY, dx: nudge }, n) => {
            const v = views[n];
            if (!v) return null;
            const g = GEO[slot];
            const { ch } = v;
            /**
             * **결정 대기 건수** — 그 챔버에서 열려 있는 Incident 수 (2026-08-08 교체).
             *
             * 구: `comments` 합계. 그 필드는 `mock/fdc.ts` 가 "**목업 전용** — 계약에 코멘트
             * 스레드 모델이 아직 없다"고 스스로 적어둔 값이었다. 화면은 "사람이 남긴 말 N건"
             * 이라 말하는데 백엔드엔 그런 데이터가 없었고, 게이트웨이가 주는 실 인시던트에도
             * `comments` 가 없어 **실운영에선 영원히 같은 숫자**가 뜬다.
             * 신: 상태로 세니 계약 안의 값이고, 「종합 현황」 승인 대기 카드와 **같은 규칙**
             * (pending·analyzing·reopen)이라 두 화면이 어긋나지 않는다.
             */
            const OPEN_ST = ["pending", "analyzing", "reopen"];
            const chOpen = incidents.filter((i) => i.chamber === ch.id && OPEN_ST.includes(i.status));
            const dec = chOpen.length;
            /**
             * 표식이 열 건 — **가장 급한 하나**. critical 우선, 그다음 score.
             * (구 "코멘트가 가장 많은 건"은 세는 값이 바뀌면서 기준이 될 이유가 없어졌다.
             *  결정 대기를 세면서 덜 급한 건을 여는 건 앞뒤가 안 맞는다.)
             */
            const decTarget = [...chOpen].sort(
              (a, b) => (b.sev === "critical" ? 1 : 0) - (a.sev === "critical" ? 1 : 0) || b.score - a.score,
            )[0];

            // 상자와 지시선 — side 에 따라 좌/우로 뺀다
            const bx = (side === "L" ? -PAD_L + EDGE : ART_W + PAD_R - BOX_W - EDGE) + (nudge ?? 0);
            const nearX = side === "L" ? bx + BOX_W : bx;
            const dir = side === "L" ? 1 : -1;
            const boxMidY = boxY + BOX_H / 2;
            const anchor = side === "L" ? g.left : g.right;
            // 지시선 = 대각선 + 상자에 수평 진입 (도면 콜아웃 관례)
            const leader = `M${anchor[0]},${anchor[1]} L${nearX + dir * 80},${boxMidY} L${nearX},${boxMidY}`;

            return (
              <g
                key={ch.id}
                className={`hs ${ch.sev}` + (ch.id === selectedId ? " sel" : "")}
                onClick={() => onEnter(ch.id)}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") onEnter(ch.id); }}
                role="button" tabIndex={0}
                aria-label={`${ch.id} ${ch.sev} — 대시보드로 이동`}
              >
                {/* 옅은 면 = 장착 표시 + 판정 영역(몸통 전체가 클릭된다). 선은 따로 — critical 점멸용 */}
                <path d={g.path} className="hsfill" />
                <path d={g.path} className="hsring" />

                <path d={leader} className="colead" />
                <circle cx={anchor[0]} cy={anchor[1]} r={11} className="coanchor" />

                <rect x={bx} y={boxY} width={BOX_W} height={BOX_H} rx={18} className="cobox" />
                {/* 상태를 **챔버 이름의 글자 배경**으로 옮겼다 — 상태어를 따로 적지 않는다.
                    이름은 어차피 시선이 먼저 닿는 곳이라, 거기에 색을 입히면 한 번에 읽힌다.
                    판은 평시에도 그려 두되 투명 — 상태가 바뀌어도 글자 위치가 흔들리지 않는다. */}
                <rect x={bx + 26} y={boxY + 22} rx={16}
                      width={ch.id.length * 38 + 48} height={82} className="coidbg" />
                <text x={bx + 50} y={boxY + 85} className="coid">{ch.id}</text>
                {/* 가한계·PM 태그 — 표의 σ 칸이 하던 일. 없으면 CH4 가 정상 챔버와 똑같아 보인다
                    (`coidbg` 는 warning·critical 만 칠한다 — provisional 은 색이 없다) */}
                {ch.tagOverride && (
                  <text x={bx + 50 + ch.id.length * 38 + 34} y={boxY + 85} className="cotag">
                    {ch.tagOverride}
                  </text>
                )}
                {/* 코멘트 — 있을 때만. 이 위험을 두고 사람이 남긴 말이 몇 건인지 */}
                {dec > 0 && decTarget && (
                  /* ⚠ `stopPropagation` — 콜아웃 몸통 전체가 이미 눌린다(`onEnter` → 챔버 상태).
                     끊지 않으면 표식을 눌러도 두 이동이 겹쳐 챔버 상태가 이긴다. */
                  <g
                    className="codec"
                    role="button" tabIndex={0}
                    onClick={(e) => { e.stopPropagation(); onOpenIncident(decTarget.id); }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") { e.stopPropagation(); onOpenIncident(decTarget.id); }
                    }}
                    aria-label={`${ch.id} 결정 대기 ${dec}건 — 가장 급한 ${decTarget.id} 로 이동`}
                  >
                    <title>
                      {`${ch.id} 결정 대기 ${dec}건 (열린 Incident)`
                        + `
클릭 → Approvals · ${decTarget.id} (가장 급한 건)`}
                    </title>
                    {/* 클릭 판정을 넓힌다 — 획 하나로는 과녁이 너무 작다 */}
                    <rect x={bx + BOX_W - 156} y={boxY + 28} width={132} height={64}
                          rx={14} className="codechit" />
                    <path d={CHECK} transform={`translate(${bx + BOX_W - 140} ${boxY + 42}) scale(1.15)`} />
                    <text x={bx + BOX_W - 30} y={boxY + 85} className="cocn">{dec}</text>
                  </g>
                )}

                {/*
                 * 콜아웃 본문 = **발동 채널 건수** (2026-08-08 — 구 감시 축 3종 σ 게이지).
                 *
                 * 구: C4·C11·C17 의 현재 σ 를 미니 관리도로 그렸다. 걷은 이유는 두 가지다.
                 *   ① **3개는 14개 중 3개**다. 알람 판정은 채널 14종(N1~N8·GAP·SUSP·ANOM·
                 *      DRIFT·B9·RTD)이 하는데 콜아웃은 센서 3종만 봤다. 실측: CH3 은 심각 4 ·
                 *      주의 5 인데 물든 축은 하나뿐이었고, 심각의 원인이 ANOM·GAP 쪽이면
                 *      **심각한 챔버가 조용해 보인다**.
                 *   ② **판정하는 코드가 따로 있었다** (`axisSev`: σ≥3 또는 open band). 나머지
                 *      화면은 전부 `cellsForChamber` 다. 오늘 대충 맞는 건 목업이 그렇게 짜여서일
                 *      뿐 구조적으로 언제든 어긋난다 — CH4 는 8종이 판정 유예인데 콜아웃엔
                 *      흔적이 없어 정상 챔버와 똑같이 보였다.
                 * (구 주석: "SHAP top3 로 오독된 적이 있다" — 그 오해도 여기서 끝난다.)
                 *
                 * 신: 헤더 칩·종합 현황 표와 **같은 소스, 같은 두 열**(심각·주의). 유예는 표가
                 * σ 칸으로 말하듯 여기서는 이름 옆 상태 태그(PROV·PM)가 말한다.
                 */}
                {(() => {
                  const c = countCells(cellsForChamber(ch.id, ch.sev));
                  // 칸 안은 **가운데 정렬** — 라벨과 숫자가 한 축에 서야 두 칸이 대칭으로 읽힌다
                  const CW = (BOX_W - 80) / 2 - 14;
                  const cell = (k: string, n: number, i: number) => (
                    <g key={k} transform={`translate(${bx + 40 + i * ((BOX_W - 80) / 2)} ${boxY + 134})`}>
                      <rect width={CW} height={144} rx={14} className="cocellbg" />
                      {/* 라벨 44 / 숫자 124 — 베이스라인 간격 80. 숫자가 78px 이라 글자 윗머리가
                          라벨 바로 밑까지 올라온다: 간격을 베이스라인이 아니라 **글자 사이**로
                          잡아야 벌어져 보인다 (구 46/110 은 실제 틈이 8px 이었다) */}
                      <text x={CW / 2} y={44} className="cocellk">{k}</text>
                      <text x={CW / 2} y={124} className={"cocellv" + (n ? " on " + k : "")}>{n || "·"}</text>
                    </g>
                  );
                  return (
                    <>
                      {/* 구 캡션("발동 채널 · 14종 중") 삭제 (2026-08-08 요청) — 인트로에서 읽히지
                          않는 크기였고, 칸 이름(심각·주의)이 이미 무엇을 세는지 말한다.
                          가로선은 남긴다: 이름 줄과 건수를 가르는 유일한 구분이다. */}
                      <line x1={bx + 28} y1={boxY + 116} x2={bx + BOX_W - 28} y2={boxY + 116} className="corule" />
                      {cell("심각", c.crit, 0)}
                      {cell("주의", c.warn, 1)}
                    </>
                  );
                })()}
              </g>
            );
          })}
        </svg>

        {artErr && (
          <div className="ovnoart">
            <b>선화 이미지가 없습니다</b>
            <code>frontend/public/tool-lineart.png</code>
            <span>이 경로에 파일을 놓으면 바로 표시됩니다 · 콜아웃과 챔버 클릭은 그대로 동작</span>
          </div>
        )}

      </div>

      <div className="introhint">
        {/* 두 경로를 다 적는다 — 챔버를 고르면 그 챔버로, 그냥 내려가면 종합으로 */}
        <span>챔버를 클릭하면 챔버 상태로 · 아래로 내리면 종합 현황</span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 8l7 7 7-7" /></svg>
      </div>
    </div>
  );
}
