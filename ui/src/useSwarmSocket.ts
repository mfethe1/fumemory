/**
 * useSwarmSocket — WebSocket hook for the Glass Box dashboard.
 * 
 * Connects to the Session Coordinator's WebSocket endpoint and
 * streams real-time SwarmEvents into React state.
 */

import { useEffect, useRef, useCallback, useState } from 'react';
import type { SwarmEvent, TaskNode, ComputeBudget, WsMessage, LaneLockInfo, DLQEntryInfo } from './types';

interface SwarmState {
  events: SwarmEvent[];
  dag: Map<string, TaskNode>;
  budget: ComputeBudget | null;
  lanes: LaneLockInfo[];
  dlq: DLQEntryInfo[];
  connected: boolean;
  error: string | null;
}

const MAX_EVENT_BUFFER = 500;

export function useSwarmSocket(wsUrl: string) {
  const wsRef = useRef<WebSocket | null>(null);
  const [state, setState] = useState<SwarmState>({
    events: [],
    dag: new Map(),
    budget: null,
    lanes: [],
    dlq: [],
    connected: false,
    error: null,
  });

  // Apply a single event to the DAG
  const applyEvent = useCallback((event: SwarmEvent) => {
    setState(prev => {
      const newDag = new Map(prev.dag);
      const taskId = event.task_id;
      const existing = newDag.get(taskId);

      // Determine new status from event type
      const statusMap: Partial<Record<SwarmEvent['event_type'], TaskNode['status']>> = {
        task_drafted: 'pending',
        bid_submitted: 'bidding',
        lease_granted: 'claimed',
        task_claimed: 'executing',
        task_completed: 'completed',
        task_failed: 'failed',
        task_cancelled: 'cancelled',
        audit_proposed: 'audit_pending',
        audit_accepted: 'completed',
        audit_rejected: 'pending',
        lease_expired: 'pending',
        circuit_breaker: 'failed',
        task_orphaned: 'orphaned',
        task_hydrated: 'hydrating',
        lane_contested: 'yielding',
        dlq_enqueued: 'dlq',
        rollback_executed: 'rolling_back',
      };

      const newStatus = statusMap[event.event_type];

      if (existing && newStatus) {
        newDag.set(taskId, {
          ...existing,
          status: newStatus,
          assigned_gateway: event.event_type === 'lease_expired' ? null : existing.assigned_gateway,
          compute_spent: existing.compute_spent + event.compute_cost,
          events: [...existing.events, event.event_id],
          completed_at: newStatus === 'completed' ? event.timestamp : existing.completed_at,
        });
      } else if (!existing && event.event_type === 'task_drafted') {
        const payload = event.payload as Record<string, unknown>;
        newDag.set(taskId, {
          task_id: taskId,
          root_prompt_id: (payload.root_prompt_id as string) ?? '',
          parent_task_id: (payload.parent_task_id as string) ?? null,
          children: [],
          title: (payload.title as string) ?? 'New Task',
          status: 'pending',
          assigned_gateway: null,
          compute_budget: (payload.compute_budget as number) ?? 0,
          compute_spent: event.compute_cost,
          events: [event.event_id],
          created_at: event.timestamp,
          completed_at: null,
        });

        // Register as child of parent
        const parentId = payload.parent_task_id as string | undefined;
        if (parentId && newDag.has(parentId)) {
          const parent = newDag.get(parentId)!;
          if (!parent.children.includes(taskId)) {
            newDag.set(parentId, {
              ...parent,
              children: [...parent.children, taskId],
            });
          }
        }
      }

      // Trim event buffer
      const newEvents = [...prev.events, event];
      if (newEvents.length > MAX_EVENT_BUFFER) {
        newEvents.splice(0, newEvents.length - MAX_EVENT_BUFFER);
      }

      return { ...prev, events: newEvents, dag: newDag };
    });
  }, []);

  // Connect WebSocket
  useEffect(() => {
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      setState(prev => ({ ...prev, connected: true, error: null }));
    };

    ws.onclose = () => {
      setState(prev => ({ ...prev, connected: false }));
      // Auto-reconnect after 3s
      setTimeout(() => {
        if (wsRef.current?.readyState === WebSocket.CLOSED) {
          wsRef.current = new WebSocket(wsUrl);
        }
      }, 3000);
    };

    ws.onerror = () => {
      setState(prev => ({ ...prev, error: 'WebSocket connection failed' }));
    };

    ws.onmessage = (msg) => {
      try {
        const parsed: WsMessage = JSON.parse(msg.data);

        switch (parsed.type) {
          case 'event':
            applyEvent(parsed.data as SwarmEvent);
            break;

          case 'dag_snapshot':
            // Full DAG replacement (on initial connect or after halt)
            setState(prev => {
              const newDag = new Map<string, TaskNode>();
              for (const node of parsed.data as TaskNode[]) {
                newDag.set(node.task_id, node);
              }
              return { ...prev, dag: newDag };
            });
            break;

          case 'budget_update':
            setState(prev => ({
              ...prev,
              budget: parsed.data as ComputeBudget,
            }));
            break;

          case 'lanes_update':
            setState(prev => ({
              ...prev,
              lanes: parsed.data as LaneLockInfo[],
            }));
            break;

          case 'dlq_update':
            setState(prev => ({
              ...prev,
              dlq: parsed.data as DLQEntryInfo[],
            }));
            break;
        }
      } catch {
        // Ignore malformed messages
      }
    };

    return () => {
      ws.close();
    };
  }, [wsUrl, applyEvent]);

  // God Mode: send SYSTEM_HALT
  const sendHalt = useCallback((rootPromptId: string, reason: string, override?: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        action: 'halt',
        root_prompt_id: rootPromptId,
        reason,
        override_instruction: override,
      }));
    }
  }, []);

  // God Mode: amend a specific task
  const sendAmend = useCallback((taskId: string, correction: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        action: 'amend',
        task_id: taskId,
        correction,
      }));
    }
  }, []);

  return {
    ...state,
    dagArray: Array.from(state.dag.values()),
    sendHalt,
    sendAmend,
    lanes: state.lanes,
    dlq: state.dlq,
  };
}
