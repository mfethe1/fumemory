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
  | 'heartbeat';

export type TaskStatus =
  | 'pending' | 'bidding' | 'claimed' | 'executing'
  | 'audit_pending' | 'completed' | 'failed' | 'cancelled';

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
} as const;

// --- WebSocket message types from Coordinator ---

export interface WsMessage {
  type: 'event' | 'dag_snapshot' | 'budget_update' | 'gateway_update' | 'halt_ack';
  data: SwarmEvent | TaskNode[] | ComputeBudget | GatewayInfo | { halted: boolean };
}
