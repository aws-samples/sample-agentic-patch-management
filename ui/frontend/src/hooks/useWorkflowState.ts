import { useReducer, useMemo } from "react";

// ── 2.1: Interfaces ────────────────────────────────────────────────

export interface InstanceState {
  instanceId: string;
  team: string;
  status: "queued" | "running" | "success" | "failed";
  elapsedMs: number | null;
}

export interface StepState {
  id: string;
  label: string;
  status: "pending" | "active" | "complete" | "failed";
  data: Record<string, unknown>;
}

export interface EnvState {
  name: string;
  status: "pending" | "active" | "complete" | "failed" | "skipped";
  steps: StepState[];
  instances: InstanceState[];
  startTime: number | null;
  endTime: number | null;
  commandId: string | null;
  decision: string | null;
  slaHours: number | null;
  windowHours: number | null;
  framework: string | null;
}

export interface WorkflowState {
  active: boolean;
  cveId: string | null;
  severity: string | null;
  startTime: number | null;
  environments: EnvState[];
  currentEnvIndex: number;
  completedAt: number | null;
}

export type WorkflowEvent =
  | { type: "workflow_start"; cve_id?: string; severity?: string; environments?: string[] }
  | { type: "workflow_step"; step: string; tool: string; status: "started" | "complete" }
  | { type: "workflow_data"; step: string; tool: string; data: Record<string, unknown> }
  | { type: "workflow_env_done"; environment: string }
  | { type: "reset" };

// ── 2.2: Default steps ─────────────────────────────────────────────

const DEFAULT_STEPS: readonly { id: string; label: string }[] = [
  { id: "discovery", label: "Discovery" },
  { id: "window_check", label: "Window Check" },
  { id: "sla_decision", label: "SLA Decision" },
  { id: "patch_execute", label: "Patch Execute" },
  { id: "health_check", label: "Health Check" },
  { id: "alarm_check", label: "Alarm Check" },
  { id: "report", label: "Report" },
];

function makeSteps(): StepState[] {
  return DEFAULT_STEPS.map((s) => ({ ...s, status: "pending", data: {} }));
}

function makeEnv(name: string): EnvState {
  return {
    name,
    status: "pending",
    steps: makeSteps(),
    instances: [],
    startTime: null,
    endTime: null,
    commandId: null,
    decision: null,
    slaHours: null,
    windowHours: null,
    framework: null,
  };
}

// ── 2.8: Initial state ─────────────────────────────────────────────

const INITIAL_STATE: WorkflowState = {
  active: false,
  cveId: null,
  severity: null,
  startTime: null,
  environments: [],
  currentEnvIndex: 0,
  completedAt: null,
};

// ── 2.3–2.8: Reducer ───────────────────────────────────────────────

function workflowReducer(state: WorkflowState, event: WorkflowEvent): WorkflowState {
  switch (event.type) {
    // 2.4: workflow_start
    case "workflow_start": {
      const envNames = event.environments ?? ["dev", "staging", "prod"];
      return {
        ...state,
        active: true,
        cveId: event.cve_id ?? state.cveId,
        severity: event.severity ?? state.severity,
        startTime: state.startTime ?? Date.now(),
        environments: envNames.map(makeEnv),
        currentEnvIndex: 0,
        completedAt: null,
      };
    }

    // 2.5: workflow_step
    case "workflow_step": {
      if (state.environments.length === 0) return state;
      const envs = [...state.environments];
      const idx = state.currentEnvIndex;
      const env = { ...envs[idx] };

      // Activate environment on first step
      if (env.status === "pending") {
        env.status = "active";
        env.startTime = Date.now();
      }

      env.steps = env.steps.map((s) => {
        if (s.id !== event.step) return s;
        return { ...s, status: event.status === "started" ? "active" : "complete" };
      });

      envs[idx] = env;
      return { ...state, environments: envs };
    }

    // 2.6: workflow_data
    case "workflow_data": {
      if (state.environments.length === 0) return state;
      const envs = [...state.environments];
      const idx = state.currentEnvIndex;
      const env = { ...envs[idx] };
      const data = event.data;

      // Store step-specific data
      env.steps = env.steps.map((s) => {
        if (s.id !== event.step) return s;
        return { ...s, data: { ...s.data, ...data } };
      });

      // Discovery: populate instances from instance_ids in tool input
      if (event.step === "discovery" && Array.isArray(data.instance_ids)) {
        env.instances = (data.instance_ids as string[]).map((id) => ({
          instanceId: id,
          team: "",
          status: "queued" as const,
          elapsedMs: null,
        }));
      }

      // Patch execute: store command_id from tool input
      if (event.step === "patch_execute") {
        if (typeof data.command_id === "string") {
          env.commandId = data.command_id;
        }
        // Update instance statuses from get_command_status results
        if (Array.isArray(data.results)) {
          const resultMap = new Map<string, string>();
          for (const r of data.results as { instance_id: string; status: string }[]) {
            resultMap.set(r.instance_id, r.status);
          }
          env.instances = env.instances.map((inst) => {
            const rawStatus = resultMap.get(inst.instanceId);
            if (!rawStatus) return inst;
            const status =
              rawStatus === "Success" ? "success" as const
              : rawStatus === "Failed" ? "failed" as const
              : rawStatus === "InProgress" ? "running" as const
              : inst.status;
            return { ...inst, status };
          });
        }
      }

      envs[idx] = env;
      return { ...state, environments: envs };
    }

    // 2.7: workflow_env_done
    case "workflow_env_done": {
      const envs = state.environments.map((env) => {
        if (env.name !== event.environment) return env;
        return { ...env, status: "complete" as const, endTime: Date.now() };
      });
      const nextIdx = Math.min(state.currentEnvIndex + 1, envs.length - 1);
      const allDone = envs.every((e) => e.status === "complete" || e.status === "skipped");
      return {
        ...state,
        environments: envs,
        currentEnvIndex: nextIdx,
        completedAt: allDone ? Date.now() : null,
        active: !allDone,
      };
    }

    // 2.8: reset
    case "reset":
      return INITIAL_STATE;

    default:
      return state;
  }
}

// ── 2.9: ETA estimation ────────────────────────────────────────────

function estimateRemainingMs(state: WorkflowState): number | null {
  const completed = state.environments.filter(
    (e) => e.status === "complete" && e.startTime && e.endTime && e.instances.length > 0
  );
  if (completed.length === 0) return null;

  const avgMsPerInstance =
    completed.reduce((sum, e) => {
      const elapsed = e.endTime! - e.startTime!;
      return sum + elapsed / e.instances.length;
    }, 0) / completed.length;

  const remaining = state.environments
    .filter((e) => e.status === "pending" || e.status === "active")
    .reduce((sum, e) => {
      if (e.status === "active") {
        // Subtract already-completed instances
        const done = e.instances.filter((i) => i.status === "success" || i.status === "failed").length;
        return sum + Math.max(0, e.instances.length - done);
      }
      return sum + Math.max(e.instances.length, 1); // at least 1 if unknown
    }, 0);

  return Math.round(avgMsPerInstance * remaining);
}

// ── 2.10: Hook export ──────────────────────────────────────────────

export function useWorkflowState() {
  const [state, dispatch] = useReducer(workflowReducer, INITIAL_STATE);
  const estimatedRemainingMs = useMemo(() => estimateRemainingMs(state), [state]);

  return { state, dispatch, estimatedRemainingMs };
}
