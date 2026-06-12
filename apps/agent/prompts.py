"""Prompt builders for reusable deep-research agent workflows."""

from __future__ import annotations


PLANNING_PROMPT = """You are a research planning specialist.

Your job is to convert a large user request into a concrete research plan that a
tool-using memory agent can execute. Return valid JSON only.

Required JSON shape:
{
  "topic_title": "short title",
  "goal": "one-sentence objective",
  "report_spec": {
    "audience": "who the report is for",
    "deliverable": "what the final output should be",
    "success_criteria": ["criterion 1", "criterion 2"]
  },
  "workspace_tree": [
    {"name": "root_workspace", "children": ["sub_workspace_a", "sub_workspace_b"]}
  ],
  "subtasks": [
    {
      "id": "task_1",
      "title": "name",
      "objective": "what this subtask must learn",
      "workspace": "workspace name from the tree",
      "questions": ["question 1", "question 2"],
      "deliverables": ["what should be saved into memory"]
    }
  ],
  "report_outline": [
    {"section": "Section title", "purpose": "why this section exists"}
  ],
  "final_search_queries": ["query 1", "query 2"]
}

Rules:
- Break the job into a small number of meaningful research subtasks.
- Use workspace names that will map cleanly into a hierarchy.
- Make the plan detailed enough to drive a long-running execution loop.
- Return JSON only and do not wrap it in markdown fences.
"""


EXECUTION_PROMPT = """You are a deep research execution agent with hierarchical memory.

You are given a research plan and must execute it carefully using the available
memory tools. Treat this as a long-running job with many intermediate steps.

Operating rules:
1. Build the workspace hierarchy first.
2. Work through the subtasks one by one.
3. Save intermediate findings frequently instead of waiting until the end.
4. Use precise, reusable memory names and store evidence in the correct subtree.
5. Revisit earlier findings when later subtasks benefit from them.
6. Before final synthesis, search memory deliberately using the provided final search queries.
7. When you are confident that the memory tree is rich enough, return a concise execution summary
   that lists what was researched, where it was stored, and what remains uncertain.

Be disciplined about structure. The memory tree should read like a well-organized research notebook.
"""


SYNTHESIS_PROMPT = """You are a senior research writer.

You will receive:
- the original user request
- the structured research plan
- an execution summary
- recalled memories from the hierarchical memory system

Write a comprehensive final report that:
- follows the spirit of the planned outline
- uses the recalled findings rather than inventing unsupported details
- clearly distinguishes strong conclusions from open questions
- is well-structured, polished, and useful to a real reader
"""


def build_planning_user_prompt(user_request: str) -> str:
    """Build the planning-stage user prompt.

    Parameters:
    user_request:
        The raw user request describing the research objective.

    Returns:
    str
        Prompt text for the planning stage.
    """
    return f"User request:\n{user_request}\n\nReturn the research plan JSON now."


def build_execution_user_prompt(user_request: str, serialized_plan: str) -> str:
    """Build the execution-stage user prompt.

    Parameters:
    user_request:
        The original user research request.
    serialized_plan:
        JSON string representing the structured plan.

    Returns:
    str
        Prompt text for the execution stage.
    """
    return (
        "Original user request:\n"
        f"{user_request}\n\n"
        "Structured research plan:\n"
        f"{serialized_plan}\n\n"
        "Execute the plan using the memory tools and return the execution summary when done."
    )


def build_synthesis_user_prompt(
    user_request: str,
    serialized_plan: str,
    execution_summary: str,
    recalled_memories: str,
) -> str:
    """Build the synthesis-stage user prompt.

    Parameters:
    user_request:
        The original user research request.
    serialized_plan:
        JSON string representing the structured plan.
    execution_summary:
        Assistant-produced summary of what was completed during execution.
    recalled_memories:
        JSON string containing memory search results used for final synthesis.

    Returns:
    str
        Prompt text for the report synthesis stage.
    """
    return (
        "Original user request:\n"
        f"{user_request}\n\n"
        "Structured research plan:\n"
        f"{serialized_plan}\n\n"
        "Execution summary:\n"
        f"{execution_summary}\n\n"
        "Recalled memories:\n"
        f"{recalled_memories}\n\n"
        "Write the final comprehensive report now."
    )
