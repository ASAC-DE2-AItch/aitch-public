/**
 * 라이브/목업/지연 표지 — KPI 소비처 공용 (S8 화면 · S1 DecisionQueue · Copilot 독).
 *
 * **배지가 거짓말하지 않게 하는 것이 이 컴포넌트의 전부다.** 이전 구현은 성공 시에만 상태를
 * 켜고 끄지 않아서, 게이트웨이가 죽어도 **얼어붙은 숫자 옆에 「● LIVE」** 가 계속 붙어 있었다.
 * 화면·프로세스·로그 어디에도 이상이 없고 값만 낡아 있는 형태라 시연 중엔 알아챌 수 없다
 * (헌법 7장 — *"조용한 상태 게이트를 노출 없이 운용하지 말 것"* · *"실값 위장 금지"*).
 *
 * 세 상태를 구분한다:
 *   ● LIVE   최근 응답이 있다
 *   ○ 지연   값은 있으나 폴링 3회분 넘게 갱신이 끊겼다 (마지막 성공 시각을 tooltip 으로)
 *   ◌ 목업   한 번도 못 받았다 — 지금 보이는 숫자는 **측정값이 아니다**
 */
export default function LiveBadge({ live, stale, lastOkAt }: {
  live: boolean;
  stale: boolean;
  lastOkAt: number | null;
}) {
  const ago = lastOkAt != null ? Math.round((Date.now() - lastOkAt) / 1000) : null;
  const [text, color, title] = !live
    ? ["◌ 목업", "var(--faint)", "라이브 데이터 없음 — 표시값은 목업이다"]
    : stale
      ? ["○ 지연", "var(--alarm)", `마지막 갱신 ${ago}초 전 — 값이 낡았을 수 있다`]
      : ["● LIVE", "var(--mint-deep)", `마지막 갱신 ${ago}초 전`];
  return (
    <span className="lgi" title={title}
          style={{ marginLeft: 8, color, display: "inline-flex" }}>
      {text}
    </span>
  );
}
