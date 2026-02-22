/**
 * Swarm Event Types — TypeScript mirror of memu/swarm_models.py
 * 
 * These types are the 1:1 frontend contract for the NATS mesh envelope.
 * If swarm_models.py changes, this file MUST change in lockstep.
 */

// --- Enums ---

export type EventType =
  | 'task_drafted' | 'task_amended' | 'task_claimed'
  | 'task_completed' | 'task_failed' | 'task_cancelled'
  | 'bid_submitted' | 'lease_granted' | 'lease_expired'
  | 'audit_proposed' | 'audit_accepted' | 'audit_rejected'
  | 'circuit_breaker' | 'system_halt' | 'system_override'
  | 'heartbeat'
  // Lane locking & fault tolerance
  | 'lane_acquired' | 'lane_released' | 'lane_contested'
  | 'task_orphaned' | 'task_hydrated' | 'checkpoint_saved'
  | 'rollback_executed'
  | 'dlq_enqueued' | 'dlq_diagnosed' | 'dlq_healed'
  | 'rpc_request' | 'rpc_response'
  // Warden / container lifecycle
  | 'suicide_signal' | 'container_started' | 'container_died'
  | 'fork_bomb_blocked' | 'blackboard_validated';

export type TaskStatus =
  | 'pending' | 'bidding' | 'claimed' | 'executing'
  | 'audit_pending' | 'completed' | 'failed' | 'cancelled'
  | 'orphaned' | 'hydrating' | 'yielding' | 'dlq' | 'rolling_back';

export type GatewayStatus = 'online' | 'offline' | 'degraded';

// --- Core Event Envelope ---

export interface SwarmEvent {
  event_id: string;
  timestamp: string;  // ISO 8601
  source_gateway: string;
  event_type: EventType;
  task_id: string;
  parent_event_id?: string | null;
  payload: Record<string, unknown>;
  context_pointer?: string | null;
  compute_cost: number;
  signature?: string | null;
}

// --- Task DAG Node ---

export interface TaskNode {
  task_id: string;
  root_prompt_id: string;
  parent_task_id?: string | null;
  children: string[];
  title: string;
  status: TaskStatus;
  assigned_gateway?: string | null;
  compute_budget: number;
  compute_spent: number;
  events: string[];
  created_at?: string | null;
  completed_at?: string | null;
  // Lane locking
  resource_lanes: string[];
  fencing_token?: number | null;
  // Fault tolerance
  rollback_instruction?: string | null;
  checkpoint_state?: string | null;
  checkpoint_timestamp?: string | null;
  failure_count: number;
  last_error?: string | null;
  required_capabilities: string[];
}

// --- Compute Budget ---

export interface ComputeBudget {
  root_prompt_id: string;
  max_tokens: number;
  max_cost_usd: number;
  tokens_spent: number;
  cost_spent_usd: number;
  is_frozen: boolean;
}

// --- Gateway Discovery ---

export interface GatewayCapability {
  name: string;
  version: string;
  description?: string | null;
  cost_per_call: number;
}

export interface GatewayInfo {
  gateway_id: string;
  capabilities: GatewayCapability[];
  status: GatewayStatus;
  max_concurrent_tasks: number;
  model_id?: string | null;
}

// --- Status → Color mapping for React Flow nodes ---

export const STATUS_COLORS: Record<TaskStatus, string> = {
  pending:       '#94a3b8', // slate
  bidding:       '#facc15', // yellow
  claimed:       '#38bdf8', // sky blue
  executing:     '#3b82f6', // blue
  audit_pending: '#f97316', // orange
  completed:     '#22c55e', // green
  failed:        '#ef4444', // red
  cancelled:     '#6b7280', // gray
  orphaned:      '#a855f7', // purple — gateway died
  hydrating:     '#06b6d4', // cyan — recovering from checkpoint
  yielding:      '#eab308', // amber — waiting for lane lock
  dlq:           '#dc2626', // deep red — poison pill
  rolling_back:  '#f59e0b', // dark amber — saga rollback
} as const;

// --- WebSocket message types from Coordinator ---

export interface LaneLockInfo {
  lane_id: string;
  gateway_id: string;
  task_id: string;
  fencing_token: number;
  expires_at: string;
}

export interface DLQEntryInfo {
  task_id: string;
  failure_count: number;
  last_error?: string;
  root_cause_diagnosis?: string | null;
  proposed_amendment?: Record<string, unknown> | null;
  dlq_entered_at: string;
}

export interface WsMessage {
  type: 'event' | 'dag_snapshot' | 'budget_update' | 'gateway_update'
    | 'halt_ack' | 'lanes_update' | 'dlq_update'
    | 'warden_status' | 'blackboard_update';
  data: SwarmEvent | TaskNode[] | ComputeBudget | GatewayInfo
    | { halted: boolean } | LaneLockInfo[] | DLQEntryInfo[]
    | WardenStatusInfo | BlackboardEntryInfo[];
}

// --- Warden / Container Lifecycle ---

export type ContainerRole = 'gateway' | 'sandbox' | 'warm_standby';
export type ContainerState = 'booting' | 'hydrating' | 'active' | 'idle' | 'draining' | 'dead';

export interface ContainerInfo {
  container_id: string;
  gateway_id: string;
  role: ContainerRole;
  state: ContainerState;
  task_id?: string | null;
  started_at: string;
  last_heartbeat?: string | null;
  crash_count: number;
  fencing_token?: number | null;
  memory_mb: number;
  cpu_pct: number;
}

export interface WardenStatusInfo {
  active_containers: ContainerInfo[];
  warm_pool: {
    target_size: number;
    current_size: number;
    pending_tasks: number;
    active_gateways: number;
    scaling_action?: string | null;
  };
  total_containers: number;
  max_containers: number;
  crash_loops: Array<{ task_id: string; count: number; last_error: string }>;
  uptime_s: number;
  last_respawn?: string | null;
}

// --- Epistemic Blackboard ---

export interface BlackboardEntryInfo {
  entry_id: string;
  key: string;
  value: string;
  category: 'fact' | 'rule' | 'warning' | 'capability';
  source_gateway: string;
  confidence: number;
  valid_from: string;
  access_count: number;
  // Hallucination Contagion Prevention (Lenny's Validation Gate)
  is_validated: boolean;
  validated_by?: string | null;
  validated_at?: string | null;
}

export const CONTAINER_STATE_COLORS: Record<ContainerState, string> = {
  booting:   '#facc15', // yellow
  hydrating: '#06b6d4', // cyan
  active:    '#22c55e', // green
  idle:      '#94a3b8', // slate (warm pool)
  draining:  '#f97316', // orange
  dead:      '#ef4444', // red
} as const;
