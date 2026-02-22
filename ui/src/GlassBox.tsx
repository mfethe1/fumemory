/**
 * Glass Box Dashboard — Real-time DAG visualization of the swarm.
 * 
 * Uses React Flow to render tasks as colored nodes.
 * WebSocket streams events in real-time.
 * God Mode button halts the entire mesh.
 */

import { useMemo, useState, useCallback } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Panel,
  type Node,
  type Edge,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import dagre from 'dagre';

import { useSwarmSocket } from './useSwarmSocket';
import { STATUS_COLORS, type TaskNode, type TaskStatus } from './types';

const WS_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws/swarm';

// --- Dagre layout ---

function layoutDag(nodes: TaskNode[]): { nodes: Node[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: 'TB', nodesep: 80, ranksep: 100 });

  const flowNodes: Node[] = [];
  const flowEdges: Edge[] = [];

  for (const task of nodes) {
    g.setNode(task.task_id, { width: 220, height: 80 });

    if (task.parent_task_id) {
      g.setEdge(task.parent_task_id, task.task_id);
      flowEdges.push({
        id: `${task.parent_task_id}->${task.task_id}`,
        source: task.parent_task_id,
        target: task.task_id,
        animated: task.status === 'executing',
      });
    }
  }

  dagre.layout(g);

  for (const task of nodes) {
    const pos = g.node(task.task_id);
    flowNodes.push({
      id: task.task_id,
      position: { x: pos.x - 110, y: pos.y - 40 },
      data: {
        label: task.title,
        status: task.status,
        gateway: task.assigned_gateway,
        budget: task.compute_budget,
        spent: task.compute_spent,
      },
      style: {
        background: STATUS_COLORS[task.status],
        color: '#fff',
        border: '2px solid rgba(255,255,255,0.3)',
        borderRadius: 8,
        padding: '12px 16px',
        fontSize: 13,
        fontWeight: 600,
        width: 220,
        textAlign: 'center' as const,
      },
    });
  }

  return { nodes: flowNodes, edges: flowEdges };
}

// --- Components ---

function BudgetMeter({ budget }: { budget: { max_cost_usd: number; cost_spent_usd: number; is_frozen: boolean } | null }) {
  if (!budget) return null;

  const pct = Math.min((budget.cost_spent_usd / budget.max_cost_usd) * 100, 100);
  const color = budget.is_frozen ? '#ef4444' : pct > 80 ? '#f97316' : '#22c55e';

  return (
    <div style={{ background: '#1e293b', padding: 16, borderRadius: 8, minWidth: 200 }}>
      <div style={{ color: '#94a3b8', fontSize: 12, marginBottom: 4 }}>COMPUTE BUDGET</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
        <span style={{ color, fontSize: 24, fontWeight: 700 }}>${budget.cost_spent_usd.toFixed(3)}</span>
        <span style={{ color: '#64748b', fontSize: 14 }}>/ ${budget.max_cost_usd.toFixed(2)}</span>
      </div>
      <div style={{ background: '#334155', borderRadius: 4, height: 6, marginTop: 8 }}>
        <div style={{ background: color, height: '100%', borderRadius: 4, width: `${pct}%`, transition: 'width 0.3s' }} />
      </div>
      {budget.is_frozen && (
        <div style={{ color: '#ef4444', fontSize: 12, marginTop: 6, fontWeight: 600 }}>
          ⚠ CIRCUIT BREAKER — Human arbitration required
        </div>
      )}
    </div>
  );
}

function EventLog({ events }: { events: Array<{ event_id: string; timestamp: string; event_type: string; source_gateway: string; task_id: string }> }) {
  const recent = events.slice(-20).reverse();
  return (
    <div style={{ background: '#0f172a', padding: 12, borderRadius: 8, maxHeight: 300, overflowY: 'auto', fontSize: 12, fontFamily: 'monospace', minWidth: 340 }}>
      <div style={{ color: '#64748b', marginBottom: 8, fontWeight: 600 }}>EVENT LOG (last 20)</div>
      {recent.map(e => (
        <div key={e.event_id} style={{ color: '#cbd5e1', borderBottom: '1px solid #1e293b', padding: '4px 0' }}>
          <span style={{ color: '#64748b' }}>{new Date(e.timestamp).toLocaleTimeString()}</span>
          {' '}
          <span style={{ color: STATUS_COLORS[e.event_type.replace('task_', '') as TaskStatus] || '#94a3b8' }}>
            {e.event_type}
          </span>
          {' '}
          <span style={{ color: '#475569' }}>← {e.source_gateway}</span>
        </div>
      ))}
    </div>
  );
}

// --- Main App ---

export default function GlassBox() {
  const { dagArray, events, budget, connected, sendHalt, sendAmend } = useSwarmSocket(WS_URL);
  const [haltReason, setHaltReason] = useState('');
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [correction, setCorrection] = useState('');

  const { nodes, edges } = useMemo(() => layoutDag(dagArray), [dagArray]);

  const rootPromptId = dagArray[0]?.root_prompt_id;

  const handleHalt = useCallback(() => {
    if (rootPromptId && haltReason.trim()) {
      sendHalt(rootPromptId, haltReason);
      setHaltReason('');
    }
  }, [rootPromptId, haltReason, sendHalt]);

  const handleAmend = useCallback(() => {
    if (selectedNode && correction.trim()) {
      sendAmend(selectedNode, correction);
      setCorrection('');
      setSelectedNode(null);
    }
  }, [selectedNode, correction, sendAmend]);

  return (
    <div style={{ width: '100vw', height: '100vh', background: '#0f172a' }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodeClick={(_, node) => setSelectedNode(node.id)}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#1e293b" gap={20} />
        <Controls />
        <MiniMap
          nodeColor={(n) => STATUS_COLORS[(n.data as Record<string, unknown>).status as TaskStatus] || '#94a3b8'}
          style={{ background: '#1e293b' }}
        />

        {/* Status bar */}
        <Panel position="top-left">
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: 8 }}>
            <div style={{
              width: 10, height: 10, borderRadius: '50%',
              background: connected ? '#22c55e' : '#ef4444',
            }} />
            <span style={{ color: '#94a3b8', fontSize: 13 }}>
              {connected ? 'LIVE' : 'DISCONNECTED'} — {dagArray.length} tasks
            </span>
          </div>
        </Panel>

        {/* Budget meter */}
        <Panel position="top-right">
          <BudgetMeter budget={budget} />
        </Panel>

        {/* God Mode */}
        <Panel position="bottom-left">
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {/* Halt controls */}
            <div style={{ display: 'flex', gap: 8 }}>
              <input
                value={haltReason}
                onChange={e => setHaltReason(e.target.value)}
                placeholder="Halt reason..."
                style={{ background: '#1e293b', color: '#fff', border: '1px solid #334155', borderRadius: 4, padding: '6px 10px', fontSize: 13, width: 240 }}
              />
              <button
                onClick={handleHalt}
                disabled={!rootPromptId || !haltReason.trim()}
                style={{ background: '#dc2626', color: '#fff', border: 'none', borderRadius: 4, padding: '6px 16px', fontWeight: 700, cursor: 'pointer', fontSize: 13, opacity: !haltReason.trim() ? 0.5 : 1 }}
              >
                ⚡ HALT SWARM
              </button>
            </div>

            {/* Amend controls (visible when node selected) */}
            {selectedNode && (
              <div style={{ display: 'flex', gap: 8 }}>
                <input
                  value={correction}
                  onChange={e => setCorrection(e.target.value)}
                  placeholder={`Amend task ${selectedNode.slice(0, 8)}...`}
                  style={{ background: '#1e293b', color: '#fff', border: '1px solid #f97316', borderRadius: 4, padding: '6px 10px', fontSize: 13, width: 240 }}
                />
                <button
                  onClick={handleAmend}
                  disabled={!correction.trim()}
                  style={{ background: '#f97316', color: '#fff', border: 'none', borderRadius: 4, padding: '6px 16px', fontWeight: 700, cursor: 'pointer', fontSize: 13 }}
                >
                  📝 AMEND
                </button>
              </div>
            )}
          </div>
        </Panel>

        {/* Event log */}
        <Panel position="bottom-right">
          <EventLog events={events} />
        </Panel>
      </ReactFlow>
    </div>
  );
}
