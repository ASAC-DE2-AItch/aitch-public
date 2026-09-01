"""validate_fixtures.py — fdc.alert mock 30건 계약 검증 (C4-1 Day1).

DoD: 30건을 AlertModel(Contract §3) 로 검증. **의도적 파손(BROKEN) 1건만 실패**하면 통과.

판정:
  · 파일명에 'BROKEN' 포함 = 실패해야 정상(의도적 파손).
  · 그 외 = 통과해야 정상.
불일치(정상이어야 할 게 실패 / 파손이어야 할 게 통과)가 하나라도 있으면 exit 1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Windows 콘솔(cp949) UnicodeEncodeError 방지 — 서술·기호 출력 UTF-8 강제
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pydantic import ValidationError  # noqa: E402

from agent_service.app.schemas.alert import AlertModel  # noqa: E402

FIXTURES_DIR = SERVICE_ROOT / "fixtures" / "alerts"


def _is_intended_broken(path: Path) -> bool:
    return "BROKEN" in path.name


def main() -> int:
    files = sorted(FIXTURES_DIR.glob("*.json"))
    if not files:
        print(f"[validate] 픽스처 없음: {FIXTURES_DIR} — generate_fixtures.py 먼저 실행")
        return 1

    passed, failed, mismatches = [], [], []
    for path in files:
        intended_broken = _is_intended_broken(path)
        try:
            AlertModel.model_validate(json.loads(path.read_text(encoding="utf-8")))
            ok = True
            err = None
        except (ValidationError, json.JSONDecodeError) as exc:
            ok = False
            err = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__

        if ok:
            passed.append(path.name)
        else:
            failed.append((path.name, err))

        # 기대와 실제 일치 여부
        if intended_broken and ok:
            mismatches.append(f"  ✗ {path.name}: 파손 설계인데 검증 통과함 (파손이 무효)")
        elif not intended_broken and not ok:
            mismatches.append(f"  ✗ {path.name}: 정상이어야 하는데 검증 실패 — {err}")

    total = len(files)
    print(f"[validate] {total}건 검사 — 통과 {len(passed)} / 실패 {len(failed)}")
    for name, err in failed:
        tag = "의도적 파손(정상)" if _is_intended_broken(Path(name)) else "예상밖 실패"
        print(f"    실패: {name}  [{tag}]  ← {err}")

    if mismatches:
        print("\n[validate] ❌ 기대 불일치:")
        print("\n".join(mismatches))
        return 1

    n_broken = sum(1 for f in files if _is_intended_broken(f))
    if n_broken != 1:
        print(f"\n[validate] ❌ 의도적 파손 파일이 정확히 1건이어야 함 (현재 {n_broken}건)")
        return 1

    print(f"\n[validate] ✅ 통과 — 정상 {total - 1}건 검증 성공, 파손 1건만 실패(설계대로)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
