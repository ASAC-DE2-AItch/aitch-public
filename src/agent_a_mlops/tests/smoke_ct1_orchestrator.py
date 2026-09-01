# -*- coding: utf-8 -*-
"""CT① 오케스트레이터 전 경로 스모크 — DB·산출물을 임시 경로로 격리해 1 사이클 완주.

DB 는 인메모리 스텁으로 대체하고(psycopg2 무의존), `control/`·`Data/ct1`·CHANGELOG 는
`--work` 아래로 리다이렉트한다 — **레포를 오염시키지 않는다**.

검증 항목:
  ① 조립 → 재학습 → 게이트 → 자동 promote 완주 (AUTO_PROMOTED)
  ② `ct_decisions` RUNNING 선점 → finalize 전이 (감사 1행, 무기록 종결 없음)
  ③ promote 신호 드롭 · champion 포인터 갱신 · CHANGELOG 1행
  ④ 멱등 — 같은 ct_id 재진입 시 skip
  ⑤ 차단기 강하(`ct1_auto_promote: false`) → SHADOW (교체 없음)
  ⑤-b 승격 기준 미달 → SKIP (champion 유지 — FAIL 로 뭉개지지 않는다)
  ⑥ dry-run(`ct1_auto_generate: false`) → DRYRUN

실행:
    python src/agent_a_mlops/tests/smoke_ct1_orchestrator.py     # --work 기본 = 임시 디렉토리
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Windows 콘솔 기본 코드페이지(cp949)에서 ✅·① 가 UnicodeEncodeError 로 죽는다 —
# **마지막 print 에서 터지면 6/6 통과해놓고 실패로 보인다**. 출력만 UTF-8 로 올린다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                       # noqa: BLE001  (파이프·리다이렉트 등)
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lean85"))
import ct1_orchestrator as orch  # noqa: E402


# ── 인메모리 DB 스텁 (ct_decisions 만 흉내) ────────────────────────────────
class _Cur:
    """psycopg2 커서 최소 구현 — 오케스트레이터가 쓰는 3개 SQL 형태만 지원."""

    def __init__(self, rows):
        self.rows, self._res, self.rowcount = rows, None, 0

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT 1 FROM ct_decisions"):
            self._res = [(1,)] if args[0] in self.rows else []
        elif s.startswith("SELECT ct_id FROM ct_decisions"):
            self._res = [(k,) for k in self.rows]
        elif s.startswith("SELECT model_version_after"):       # GC 의 ct_decisions 대조
            self._res = [(v["after"],) for v in self.rows.values()
                         if v.get("status") in ("PROMOTED", "AUTO_PROMOTED") and v.get("after")]
        elif s.startswith("INSERT INTO ct_decisions"):
            # `ON CONFLICT DO NOTHING RETURNING ct_id` 시맨틱: **새로 심은 쪽만** 행을
            # 돌려받는다 (claim_running 의 원자적 승패 판정 근거 — 리뷰 S9).
            ct_id = args[0]
            fresh = ct_id not in self.rows
            if fresh:
                self.rows[ct_id] = {"status": args[3] if len(args) > 3 else "RUNNING",
                                    "args": args, "after": None, "rmse_before": None,
                                    "rmse_after": None, "cc": None}
            self.rowcount = 1 if fresh else 0
            self._res = [(ct_id,)] if fresh else []
        elif s.startswith("UPDATE ct_decisions"):
            ct_id = args[-1]
            if ct_id in self.rows:
                self.rows[ct_id].update({"status": args[0], "after": args[1],
                                         "reason": args[2], "rmse_before": args[3],
                                         "rmse_after": args[4], "cc": args[5]})
                self.rowcount = 1
        else:
            raise AssertionError(f"스텁이 모르는 SQL: {s[:80]}")

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res or [])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    """커밋·클로즈를 무시하는 접속 스텁."""

    def __init__(self):
        self.rows: dict = {}

    def cursor(self):
        return _Cur(self.rows)

    def commit(self):
        pass

    def close(self):
        pass


_MARKER = ".ct1smoke"          # 스모크 산출물 표식 — 남의 디렉토리 오폭 방지


def _prepare_work(work: Path) -> None:
    """`work` 를 **매 실행 새로** 만든다 (재실행 가능성).

    재사용하면 이전 실행의 champion 포인터·모델이 남아 ① 부트스트랩 시나리오가 성립하지
    않는다 — 2회차부터 개선 0% 로 SKIP 이 나서 스모크가 깨진다. 원인이 코드가 아니라
    잔여물인데 실패 메시지는 게이트를 가리켜, 디버깅을 엉뚱한 데로 보낸다.

    삭제는 **스모크가 남긴 마커가 있을 때만** 한다. 마커 없는 비어있지 않은 경로는
    거부한다 — `--work` 오타 한 번이 남의 디렉토리를 지우는 일은 없어야 한다.
    """
    if work.exists():
        # 마커 외에 **스모크 고유 레이아웃**도 인정한다 — 마커 도입 이전에 만들어진
        # 디렉토리가 영구 차단되면, 고치려던 재실행 불가가 형태만 바꿔 남는다.
        ours = ((work / _MARKER).exists()
                or (work / "control" / "ct1").exists()
                or (work / "models" / "c65_predictor").exists())
        if not ours and any(work.iterdir()):
            raise SystemExit(
                f"--work 가 비어 있지 않고 스모크 산출물도 아니다: {work}\n"
                f"  스모크는 이 경로를 통째로 지운다 — 다른 경로를 지정하거나 직접 비울 것.")
        shutil.rmtree(work)
    work.mkdir(parents=True)
    (work / _MARKER).touch()


def _redirect(work: Path) -> None:
    """오케스트레이터의 쓰기 경로를 전부 `work` 아래로 (레포 무오염)."""
    orch.CONTROL_DIR = work / "control" / "ct1"
    orch.CHAMPION_PTR = orch.CONTROL_DIR / "champion.json"
    orch.LOCK_PATH = orch.CONTROL_DIR / "ct1_orchestrator.lock"
    orch.STATE_PATH = orch.CONTROL_DIR / "ct1_state.json"
    orch.WORK_DIR = work / "data"
    orch.REPO_ROOT = work                       # CHANGELOG·GC 대상 격리
    (work / "models").mkdir(parents=True, exist_ok=True)
    orch.lp._default_store_dir = lambda: work / "models" / "c65_predictor" / "v2_lean85"


def _params(repo: Path, *, auto_generate=True, auto_promote=True) -> dict:
    """스모크용 config — 실물 params.yaml 을 건드리지 않는다."""
    lean85 = repo / "src" / "agent_a_mlops" / "lean85"
    store = orch.lp._default_store_dir()
    return {
        "ct1_auto_generate": auto_generate,
        "ct1_auto_promote": auto_promote,
        "ct1_cmd_cwd": str(lean85),
        # REPO_ROOT 를 work 로 리다이렉트했으므로 원천은 **절대경로**로 준다
        # (`Path(work) / 절대경로` = 절대경로).
        "ct1_data_glob": str(repo / "Data" / "synthetic_1y" / "synth_2019-1*.csv.gz"),
        "ct1_retrain_timeout_sec": 1800,
        "ct1_gc_keep_unpromoted_days": 14,
        "ct1_max_stale_champion_days": 3650,
        "ct1_gate_min_train_wafers": 500,
        "ct1_assemble_cmd": ("python ct1_assemble_trainset.py --data {data} "
                             "--pm-log {pm_log} --now {now} --out {out} --stamp {stamp} "
                             "--window-days 45 --buffer-days 3 --min-train-wafers 500"),
        "ct1_retrain_cmd": ("python retrain_lean85.py --data {data} --pm-log {pm_log} "
                            f"--tag {{tag}} --out {store} "
                            "--window-meta {window_meta} --result-json {result}"),
        "ct1_validate_cmd": ("python validate_lean85.py --challenger {challenger} "
                             "--champion {champion} --eval-data {eval_data} "
                             "--window-meta {window_meta} --pm-log {pm_log} --out {out}"),
    }


def main() -> int:
    """스모크 실행 — 실패 시 AssertionError 로 즉시 드러낸다."""
    ap = argparse.ArgumentParser()
    # 기본값을 "/tmp" 로 두면 Windows 에서 C:\tmp 를 새로 만든다 (팀 주 플랫폼).
    ap.add_argument("--work", default=str(Path(tempfile.gettempdir()) / "ct1smoke_orch"))
    ap.add_argument("--now", default="2019-11-30T02:00:00")
    a = ap.parse_args()

    repo = Path(__file__).resolve().parents[3]
    work = Path(a.work)
    _prepare_work(work)                           # 잔여물 제거 — 2회차 실행도 같은 결과여야 한다
    _redirect(work)
    orch.lp.REPO_ROOT = repo                      # 원천 데이터는 실제 레포에서 읽는다
    # ★aware UTC 로 넘긴다 — 상주 루프가 실제로 주는 형태이고, 리뷰 B1(tz 혼재 TypeError)
    #  의 회귀를 스모크에서도 잡기 위함이다.
    now = datetime.fromisoformat(a.now).replace(tzinfo=timezone.utc)

    conn = _Conn()
    orch._db = lambda: conn                       # DB 스텁 주입

    # ⑥ dry-run
    st = orch.handle_trigger(_params(repo, auto_generate=False), now=now,
                             trigger="daily_sliding", tag="daily")
    assert st == "DRYRUN", st
    assert any(v["status"] == "DRYRUN" for v in conn.rows.values()), conn.rows
    print("✅ ⑥ dry-run → DRYRUN 기록")

    # ① ~ ③ 자동 promote 완주
    p = _params(repo)
    st = orch.handle_trigger(p, now=now, trigger="daily_sliding", tag="daily")
    assert st == "AUTO_PROMOTED", f"기대 AUTO_PROMOTED, 실제 {st} — 로그 확인"
    row = [v for v in conn.rows.values() if v["status"] == "AUTO_PROMOTED"][0]
    assert row["after"] and row["rmse_after"] is not None, row
    print(f"✅ ①② 완주 + 감사 행: after={row['after']} "
          f"rmse {row['rmse_before']} → {row['rmse_after']} cc={row['cc']}")

    sigs = sorted(orch.CONTROL_DIR.glob("promote_*.json"))
    assert sigs, "promote 신호 미드롭"
    sig = json.loads(sigs[0].read_text(encoding="utf-8"))
    ptr = json.loads(orch.CHAMPION_PTR.read_text(encoding="utf-8"))
    assert Path(sig["model_dir"]).exists() and ptr["model_version"] == row["after"]
    changelog = (work / "models" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "CT①(lean85) promote" in changelog
    print(f"✅ ③ 신호 {sigs[0].name} · champion 포인터 {ptr['model_version']} · CHANGELOG 1행")

    # ④ 멱등 — 같은 날 같은 SEQ 로 강제 재진입
    before_n = len(conn.rows)
    assert orch.claim_running(conn, list(conn.rows)[0], "daily_sliding") is False
    assert len(conn.rows) == before_n
    print("✅ ④ 멱등 — 기존 ct_id 재진입 skip")

    # ⑤ 차단기 강하 → SHADOW.
    #    같은 데이터로 재학습하면 challenger ≡ champion 이라 개선 0% → SKIP 이 정상이다
    #    (그 자체가 D 군이 작동한다는 증거). 차단기 분기를 보려면 게이트를 PASS 시켜야
    #    하므로 임계만 음수로 낮춘 임시 params 를 물린다.
    lax = work / "params_lax.yaml"
    lax.write_text("ct:\n  ct1_promote_min_rmse_gain_pct: -1.0\n"
                   "  ct1_gate_min_eval_wafers: 100\n"
                   "  ct1_gate_min_train_wafers: 500\n", encoding="utf-8")
    p5 = _params(repo, auto_promote=False)
    p5["ct1_validate_cmd"] += f" --params {lax}"
    st = orch.handle_trigger(p5, now=now, trigger="daily_sliding", tag="daily")
    assert st == "SHADOW", f"기대 SHADOW, 실제 {st}"
    assert len(sorted(orch.CONTROL_DIR.glob("promote_*.json"))) == 1, "차단기인데 신호가 늘었다"
    assert json.loads(orch.CHAMPION_PTR.read_text(encoding="utf-8"))["model_version"] == \
        ptr["model_version"], "차단기인데 champion 포인터가 바뀌었다"
    print("✅ ⑤ 차단기 강하 → SHADOW (신호·포인터 불변)")

    # ⑤-b 승격 기준 미달 → SKIP (동일 데이터 재학습 = 개선 0%)
    st = orch.handle_trigger(_params(repo), now=now, trigger="daily_sliding", tag="daily")
    assert st == "SKIP", f"기대 SKIP, 실제 {st}"
    print("✅ ⑤-b 개선 0% → SKIP (champion 유지)")

    print("\n스모크 6/6 통과 —", work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
