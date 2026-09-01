// 화면 정의서 v1.3의 화면 목록 총괄 — 라우트 단일 소스 (헌법 6-4: 화면 ID 주석 병기)
export interface ScreenDef {
  id: string; // 화면 정의서 ID (S0~S11)
  path: string;
  label: string;
  group: "OPERATION" | "SIMULATOR";
  priority: "Must" | "Should" | "Could";
}

export const SCREENS: ScreenDef[] = [
  { id: "S1", path: "/", label: "실시간 모니터링", group: "OPERATION", priority: "Must" },
  { id: "S2", path: "/spc", label: "SPC / TTTM 차트", group: "OPERATION", priority: "Must" },
  { id: "S3", path: "/incidents", label: "승인 큐", group: "OPERATION", priority: "Must" },
  // S4(Incident 상세)·S5(수정 승인 모달)는 S3에서 진입하는 하위 라우트/모달
  { id: "S6", path: "/dispositions", label: "Wafer Disposition", group: "OPERATION", priority: "Must" },
  { id: "S7", path: "/qual", label: "Qual 검증", group: "OPERATION", priority: "Must" },
  { id: "S8", path: "/approvals", label: "승인 이력 · KPI", group: "OPERATION", priority: "Should" },
  // S9는 상시 독(CopilotDock)으로 승격 (v1.3) — 전체화면 라우트는 보조
  { id: "S9", path: "/query", label: "Agent Copilot", group: "OPERATION", priority: "Must" },
  { id: "S10", path: "/models", label: "모델 / CT 상태", group: "OPERATION", priority: "Could" },
  { id: "S11", path: "/simulator", label: "시나리오 시뮬레이터", group: "SIMULATOR", priority: "Must" },
];
