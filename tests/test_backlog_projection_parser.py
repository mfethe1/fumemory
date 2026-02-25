from memu.backlog_projection import parse_backlog_markdown


def test_parse_backlog_markdown_extracts_task_fields():
    md = """
## P0
- **Task:** OAuth/auth reliability + end-to-end sign-in green (OWNER: Macklemore)
  - STATUS: in_progress
  - NEXT ACTION: run live Google OAuth smoke matrix with real Supabase project creds
  - BLOCKER: missing test OAuth credentials in this cron environment.
"""
    tasks = parse_backlog_markdown(md)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.owner == "Macklemore"
    assert task.lane == "infra"
    assert task.status == "in_progress"
    assert "OAuth/auth reliability" in task.title
    assert "run live Google OAuth smoke matrix" in (task.next_action or "")
    assert "missing test OAuth credentials" in (task.blocker or "")
    assert task.task_id


def test_parse_backlog_markdown_handles_multiple_tasks():
    md = """
- **Task:** One (OWNER: Team)
  - STATUS: active
- **Task:** Two (OWNER: Lenny)
  - STATUS: pending
"""
    tasks = parse_backlog_markdown(md)
    assert len(tasks) == 2
    assert tasks[0].lane == "shared"
    assert tasks[1].lane == "qa"
