"""Ollama vs vLLM 서빙 엔진 비교 하네스 (C4-3 잔여 / config 합의안 D21 검증).

배경: D13(서빙=vLLM+guided decoding)은 **모델 확정 전(7/06)** 원칙으로 정해졌고, 실제 벤치(D9)는
전부 Ollama로 수행됐다 → "vLLM+guided_json 이 qwen3.6 에서 실제 도나"가 D21 미결로 남음.
동시에 "우리 부하가 vLLM 을 정당화하나"도 미측정이었다. 이 하네스가 둘 다 잰다.

측정 대상 (같은 박스·같은 GPU 에서 순차 실행 — 48GB 에 FP8 35B + Ollama Q4 동시 적재 불가):
  ⓐ 기능   — guided_json 동작 여부(vLLM 전용), JSON 유효율
  ⓑ 부하   — storm(알람 다건 동시) × 조수 3종 팬아웃 → 동시 호출 시 지연 분포(p50/p95)·처리량

부하 모형: 우리 파이프라인은 alert 1건 → tool 3종 병렬(헌법 1-2)이라 **동시 LLM 호출 = alert × 3**.
storm_size N 이면 N×3 개를 동시에 던진다.

사용:
  python -m agent_service.compare_serving_engines \\
      --ollama-url http://<ip>:11434 --vllm-url http://<ip>:8000 \\
      --storm-size 8 --report bench.md --dump bench.json
  # 한쪽만: --skip-vllm / --skip-ollama (엔진 전환 사이에 나눠 실행)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # src/ 를 경로에 추가

from agent_service.app.config import load_settings  # noqa: E402
from agent_service.app.llm.prompts import build_messages  # noqa: E402
from agent_service.app.llm.vllm import VllmBackend  # noqa: E402

# 생성 파라미터는 **params.yaml 이 단일 소스**다 (헌법 6-1 — 하드코딩 금지).
# 두 엔진에 동일 적용되므로 공정 비교가 성립한다.
_SETTINGS = load_settings()
TEMPERATURE = float(_SETTINGS.require("llm.temperature"))            # D10
SEED = int(_SETTINGS.require("llm.seed"))                            # D12
MAX_OUTPUT_TOKENS = int(_SETTINGS.require("llm.max_output_tokens"))  # D11
SLOW_PATH_BUDGET_SEC = float(_SETTINGS.require("agent.slow_path_max_sec"))  # D4 — 합격선

# 측정용 타임아웃은 **운영 예산(D14 call_timeout_sec)과 다른 목적**이다.
# 운영은 "예산 넘으면 끊는다"이고, 여기는 "부하 시 얼마나 밀리는지 재는" 게 목적이라 길게 잡는다.
# (예산으로 끊어버리면 지연 분포를 못 재고 전부 타임아웃으로만 보인다.) --call-timeout 으로 조정.
DEFAULT_BENCH_TIMEOUT_SEC = 240.0

# thinking 억제 — 엔진마다 방법이 다르다(실측).
#   vLLM  : chat_template_kwargs 로 템플릿 레벨 억제 가능
#   Ollama: /v1 경로에선 think/no_think/chat_template_kwargs 모두 무시됨 → 억제 불가, 토큰으로 커버
VLLM_EXTRA = {"chat_template_kwargs": {"enable_thinking": False}}
OLLAMA_EXTRA: dict[str, Any] = {}

# 팬아웃 배수 — alert 1건당 동시 LLM 호출 수 (조수 3종, 헌법 1-2)
FANOUT = 3

# 라이브러리 §1 레시피 역할 프롬프트(축약) — 3조수 호출의 대표 크기.
# 부하 측정이 목적이라 tool 별 프롬프트 차이보다 "동시 N개 × 유사 토큰 크기"가 중요하다.
ROLE_PROMPT = """[역할] 공정 조건 이탈에 대한 레시피 튜닝안 리포트를 작성한다.

[작성 규칙]
1. 튜닝 수치는 입력 tuning 을 그대로 인용한다. 재계산 금지.
2. |delta_pct| > 3.0 이면 튜닝안 대신 escalate_reason 에 사유.
3. rationale 3층: ① SPC 위반 ② SHAP+KB 물리축 ③ CASE 과거조치.
4. SHAP 상위 센서가 knob_map 에 없으면(C12·C17 등) regime_signal.

[JSON 스키마로만 응답]"""

SCHEMA_EXAMPLE = """{
  "report_id": "RCP-20260713-SIMCH3-001",
  "option_type": "recipe_option",
  "parameter_id": "C4",
  "value_proposed": 39.2,
  "delta_pct": -2.0,
  "rationale": "① ... [SPC] ② ... [KB] ③ ... [CASE]",
  "hypothesis": "process_condition_shift",
  "confidence": 0.78,
  "uncertainty": "..."
}"""

# guided_json 용 스키마 — vLLM 이 토큰 레벨로 강제(D13). Ollama 는 미지원이라 전달하지 않는다.
GUIDED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "report_id": {"type": "string"},
        "option_type": {"type": "string"},
        "parameter_id": {"type": ["string", "null"]},
        "value_proposed": {"type": ["number", "null"]},
        "delta_pct": {"type": ["number", "null"]},
        "rationale": {"type": "string"},
        "hypothesis": {
            "type": "string",
            "enum": ["process_condition_shift", "regime_signal", "insufficient_evidence"],
        },
        "confidence": {"type": "number"},
        "uncertainty": {"type": "string"},
    },
    "required": ["option_type", "rationale", "hypothesis", "confidence", "uncertainty"],
}


@dataclass
class CallResult:
    """LLM 호출 1건의 측정 결과."""

    latency_sec: float
    ok: bool
    json_valid: bool
    error: Optional[str] = None
    raw_len: int = 0


@dataclass
class EngineResult:
    """엔진 1종의 storm 부하 측정 결과."""

    engine: str
    model: str
    guided_json: bool
    max_tokens: int
    storm_size: int
    concurrent_calls: int
    wall_clock_sec: float = 0.0
    calls: list[CallResult] = field(default_factory=list)

    @property
    def latencies(self) -> list[float]:
        return [c.latency_sec for c in self.calls if c.ok]

    def pct(self, p: float) -> float:
        """지연 분위수(초). 표본 없으면 0."""
        vals = sorted(self.latencies)
        if not vals:
            return 0.0
        idx = min(int(len(vals) * p), len(vals) - 1)
        return vals[idx]

    def summary(self) -> dict[str, Any]:
        ok = [c for c in self.calls if c.ok]
        valid = [c for c in self.calls if c.json_valid]
        return {
            "engine": self.engine,
            "model": self.model,
            "guided_json": self.guided_json,
            "max_tokens": self.max_tokens,
            "storm_size": self.storm_size,
            "concurrent_calls": self.concurrent_calls,
            "wall_clock_sec": round(self.wall_clock_sec, 2),
            "success_rate": round(len(ok) / len(self.calls), 3) if self.calls else 0.0,
            "json_valid_rate": round(len(valid) / len(self.calls), 3) if self.calls else 0.0,
            "latency_p50": round(self.pct(0.50), 2),
            "latency_p95": round(self.pct(0.95), 2),
            "latency_mean": round(statistics.mean(self.latencies), 2) if self.latencies else 0.0,
            "latency_max": round(max(self.latencies), 2) if self.latencies else 0.0,
            "throughput_req_per_sec": (
                round(len(ok) / self.wall_clock_sec, 2) if self.wall_clock_sec else 0.0
            ),
            "errors": [c.error for c in self.calls if c.error][:3],
        }


def load_alerts(fixtures_dir: Path, storm_size: int) -> list[dict[str, Any]]:
    """게이트 통과(context_score >= 31) alert 를 storm_size 만큼 순환 수집.

    below_gate 픽스처는 실제 파이프라인에서 LLM 을 안 태우므로(B7 게이트) 부하 모형에서 제외한다.
    """
    files = sorted(p for p in fixtures_dir.glob("*.json") if "BROKEN" not in p.name)
    alerts: list[dict[str, Any]] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("context_score", 0) >= 31:  # B7 게이트
            alerts.append(data)
    if not alerts:
        raise SystemExit(f"게이트 통과 alert 없음: {fixtures_dir}")
    return [alerts[i % len(alerts)] for i in range(storm_size)]


def build_payload(alert: dict[str, Any]) -> str:
    """tool 입력 직렬화 — B5-4/RAG 미연결이라 근거는 비운다(C5-1 인터페이스 스텁 전 단계).

    부하 측정 목적상 중요한 건 입력 토큰 크기와 동시성이지 근거 유무가 아니다.
    """
    return json.dumps(
        {"alert": alert, "tuning": None, "knob_map": None, "kb_hits": [], "cases": []},
        ensure_ascii=False,
    )


async def one_call(
    backend: VllmBackend,
    alert: dict[str, Any],
    guided: bool,
    max_tokens: int,
    timeout: float,
) -> CallResult:
    """LLM 호출 1건 — 지연·성공·JSON 유효성 측정."""
    messages = build_messages(ROLE_PROMPT, build_payload(alert), schema_example=SCHEMA_EXAMPLE)
    started = time.perf_counter()
    try:
        raw = await backend.complete(
            messages,
            guided_json=GUIDED_SCHEMA if guided else None,
            timeout=timeout,
            temperature=TEMPERATURE,
            seed=SEED,
            max_output_tokens=max_tokens,
        )
    except Exception as exc:  # 타임아웃·연결·서버오류 — 부하 하에서의 실패도 측정 대상
        return CallResult(time.perf_counter() - started, False, False, f"{type(exc).__name__}: {exc}"[:200])
    elapsed = time.perf_counter() - started

    text = raw.strip()
    if text.startswith("```"):  # guided_json 없으면 코드펜스가 붙을 수 있다(client._extract_json 과 동일 방어)
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        json.loads(text)
        return CallResult(elapsed, True, True, None, len(raw))
    except Exception:
        return CallResult(elapsed, True, False, "json_parse_failed", len(raw))


async def run_engine(
    engine: str,
    base_url: str,
    model: str,
    alerts: list[dict[str, Any]],
    guided: bool,
    max_tokens: int,
    timeout: float,
    extra: dict[str, Any] | None = None,
) -> EngineResult:
    """엔진 1종에 storm 부하를 던지고 측정. alert 1건당 FANOUT 개 동시 호출."""
    backend = VllmBackend(base_url=base_url, model=model, timeout=timeout, extra_payload=extra)
    result = EngineResult(
        engine=engine,
        model=model,
        guided_json=guided,
        max_tokens=max_tokens,
        storm_size=len(alerts),
        concurrent_calls=len(alerts) * FANOUT,
    )

    # 워밍업 1건 — 첫 호출의 모델 로딩/컴파일 비용을 측정에서 제외.
    print(f"[{engine}] 워밍업...", flush=True)
    warm = await one_call(backend, alerts[0], guided, max_tokens, timeout)
    print(f"[{engine}] 워밍업 {'OK' if warm.ok else 'FAIL: ' + str(warm.error)} ({warm.latency_sec:.1f}s)", flush=True)

    print(f"[{engine}] storm: alert {len(alerts)}건 × 팬아웃 {FANOUT} = 동시 {result.concurrent_calls}개 호출", flush=True)
    tasks = [one_call(backend, a, guided, max_tokens, timeout) for a in alerts for _ in range(FANOUT)]
    started = time.perf_counter()
    result.calls = await asyncio.gather(*tasks)
    result.wall_clock_sec = time.perf_counter() - started
    await backend.aclose()

    s = result.summary()
    print(
        f"[{engine}] 완료 — wall {s['wall_clock_sec']}s / p50 {s['latency_p50']}s / "
        f"p95 {s['latency_p95']}s / 성공 {s['success_rate']} / JSON {s['json_valid_rate']}",
        flush=True,
    )
    return result


def render_report(results: list[EngineResult], storm_size: int) -> str:
    """비교 리포트(markdown) 생성 — 엔진 결정 근거로 쓸 수 있게."""
    used_tokens = results[0].max_tokens if results else MAX_OUTPUT_TOKENS
    token_note = (
        f"{used_tokens}"
        if used_tokens == MAX_OUTPUT_TOKENS
        else f"{used_tokens} ⚠️ (params.yaml D11={MAX_OUTPUT_TOKENS} 오버라이드 — 사유를 아래 한계에 기록)"
    )
    lines = [
        "# Ollama vs vLLM 서빙 비교 (C4-3 잔여 / D21 검증)",
        "",
        f"> storm_size={storm_size} · 팬아웃 {FANOUT} (alert 1건 → 조수 3종 병렬, 헌법 1-2)",
        f"> 동시 호출 = {storm_size * FANOUT}개",
        f"> 생성 파라미터는 **params.yaml 단일 소스**(헌법 6-1): temp={TEMPERATURE}(D10) · seed={SEED}(D12) · max_tokens={token_note}(D11)",
        f"> 합격선: p95 < slow-path 예산 **{SLOW_PATH_BUDGET_SEC}s**(D4 `agent.slow_path_max_sec`)",
        "> 같은 박스·같은 GPU 에서 순차 측정 (48GB VRAM 에 두 엔진 동시 적재 불가).",
        "",
        "## 결과",
        "",
        "| 엔진 | 모델 | guided_json | wall clock | p50 | p95 | max | 처리량(req/s) | 성공률 | JSON유효율 | D4 합격 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        s = r.summary()
        passed = "✅" if (s["latency_p95"] and s["latency_p95"] < SLOW_PATH_BUDGET_SEC) else "❌"
        lines.append(
            f"| **{s['engine']}** | `{s['model']}` | {'✅' if s['guided_json'] else '—'} | "
            f"{s['wall_clock_sec']}s | {s['latency_p50']}s | {s['latency_p95']}s | {s['latency_max']}s | "
            f"{s['throughput_req_per_sec']} | {s['success_rate']} | {s['json_valid_rate']} | {passed} |"
        )
    lines += ["", "## 판단 재료", ""]
    for r in results:
        s = r.summary()
        if s["errors"]:
            lines.append(f"- **{s['engine']} 오류 표본**: {s['errors']}")
    lines += [
        "",
        "- **guided_json**: vLLM 만 토큰 레벨 강제(D13). Ollama 는 프롬프트 강제 + `_extract_json` 방어에 의존.",
        "- **부하 판단**: 동시 호출 시 p95 가 slow-path 예산(params.yaml `agent.slow_path_sec`)을 넘는지가 핵심.",
        "- 이 수치는 **엔진 확정(D13 유지 vs Ollama 폴백)의 근거 자료** — 최종 결정은 PM 과 함께.",
        "",
        "## 한계 (정직하게)",
        "",
        "- **정밀도가 다르다 — 순수 엔진 비교가 아님**: vLLM=FP8(~35GB) / Ollama=GGUF Q4_K_M(~23GB).",
        "  Q4 는 가중치가 작아 메모리 트래픽이 적으므로 **지연 우위의 일부는 엔진이 아니라 양자화 덕**일 수 있다.",
        "  다만 이 조합은 **각 엔진의 현실적 배포 구성**(vLLM→L40S FP8 / Ollama→GGUF Q4)이므로,",
        "  '엔진 자체'가 아니라 **'실제 배포 구성끼리'의 비교**로 읽어야 한다. 엔진 결정에는 후자가 맞는 질문.",
        "- **품질(판정 정확도)은 여기서 안 잰다** — 이 하네스는 서빙(지연·처리량·JSON)만. 모델 품질은 D9 벤치 소관.",
        "- 단일 런 · 스팟 인스턴스 · 네트워크 RTT 포함(노트북→서울 리전). 두 엔진에 동일 조건이라 상대 비교는 유효.",
    ]
    return "\n".join(lines) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser(description="Ollama vs vLLM 서빙 비교 (C4-3/D21)")
    ap.add_argument("--ollama-url", default="", help="예: http://1.2.3.4:11434")
    ap.add_argument("--vllm-url", default="", help="예: http://1.2.3.4:8000")
    ap.add_argument("--ollama-model", default="qwen3.6:35b-a3b")
    ap.add_argument("--vllm-model", default="Qwen/Qwen3.6-35B-A3B-FP8")
    ap.add_argument("--storm-size", type=int, default=8, help="동시 alert 수 (동시 호출 = ×3)")
    ap.add_argument(
        "--max-tokens",
        type=int,
        default=MAX_OUTPUT_TOKENS,
        help=f"생성 토큰 상한. 기본 = params.yaml D11 ({MAX_OUTPUT_TOKENS}). "
        "thinking 모델 특성 조사 등 예외 시에만 오버라이드하고 리포트에 사유를 남길 것.",
    )
    ap.add_argument(
        "--call-timeout",
        type=float,
        default=DEFAULT_BENCH_TIMEOUT_SEC,
        help=f"측정용 호출 타임아웃(초). 기본 {DEFAULT_BENCH_TIMEOUT_SEC} — 운영 예산(D14)이 아니라 "
        "부하 지연 분포를 재기 위한 값.",
    )
    ap.add_argument("--fixtures", default="", help="alert 픽스처 디렉토리 (기본: fixtures/alerts)")
    ap.add_argument("--report", default="", help="markdown 리포트 출력 경로")
    ap.add_argument("--dump", default="", help="raw json 결과 경로")
    ap.add_argument("--skip-ollama", action="store_true")
    ap.add_argument("--skip-vllm", action="store_true")
    args = ap.parse_args()

    fixtures = Path(args.fixtures) if args.fixtures else Path(__file__).resolve().parent / "fixtures" / "alerts"
    alerts = load_alerts(fixtures, args.storm_size)
    print(f"alert {len(alerts)}건 로드 (게이트 통과분, {fixtures})")

    results: list[EngineResult] = []
    # 순차 실행 — 같은 GPU 를 나눠 쓰므로 동시에 재면 서로 간섭한다.
    if args.vllm_url and not args.skip_vllm:
        results.append(
            await run_engine(
                "vLLM",
                args.vllm_url,
                args.vllm_model,
                alerts,
                guided=True,
                max_tokens=args.max_tokens,
                timeout=args.call_timeout,
                extra=VLLM_EXTRA,
            )
        )
    if args.ollama_url and not args.skip_ollama:
        results.append(
            await run_engine(
                "Ollama",
                args.ollama_url,
                args.ollama_model,
                alerts,
                guided=False,
                max_tokens=args.max_tokens,
                timeout=args.call_timeout,
                extra=OLLAMA_EXTRA,
            )
        )

    if not results:
        raise SystemExit("측정 대상 없음 — --ollama-url / --vllm-url 중 하나는 필요")

    report = render_report(results, args.storm_size)
    print("\n" + report)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
        print(f"리포트 저장: {args.report}")
    if args.dump:
        Path(args.dump).write_text(
            json.dumps([r.summary() for r in results], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"raw 저장: {args.dump}")


if __name__ == "__main__":
    asyncio.run(main())
