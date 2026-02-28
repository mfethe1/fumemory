-- Migration 005: Expand memory_type constraint to include new A-MEM types
-- Old types: fact, decision, lesson, pattern, failure
-- New types: observation, reflection, plan, goal, user_action, external
-- Both sets are accepted for backwards compatibility

ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_memory_type_check;
ALTER TABLE memories ADD CONSTRAINT memories_memory_type_check
  CHECK (memory_type IN (
    'fact', 'decision', 'lesson', 'pattern', 'failure',
    'observation', 'reflection', 'plan', 'goal', 'user_action', 'external'
  ));
