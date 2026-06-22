# Apps

This directory contains runnable application examples built on top of the Genie library.

## Deep Research Agent

The example deep research agent is an interactive CLI that:

- plans a large research task into smaller subtasks
- stores intermediate work in Genie’s hierarchical Redis memory
- recalls saved memories before synthesis
- prints a final report plus a verification summary showing whether the run looked genuinely multi-step

### Prerequisites

Before running the example, make sure you have:

1. A running Redis instance.
2. An OpenAI API key stored in `openai.env` as `OPENAI_API_KEY` at the project root.
3. The project dependencies installed in the Conda environment defined in `agents.md`.

`apps/run_agent.py` loads credentials from `openai.env` and connects to Redis at `redis://localhost:6379/0` by default.

### Run The Agent

From the project root:

```bash
python apps/run_agent.py
```

You will be prompted for a topic, for example:

```text
Research topic: humidity effect on plant photosynthesis
```

### What The Agent Does

For each run, the agent:

1. Builds a structured research plan.
2. Creates a dedicated run subtree in Redis.
3. Executes the plan with Genie memory tools.
4. Saves intermediate memories during execution.
5. Recalls stored memories from the run subtree before writing the final report.
6. Prints a verification summary showing whether the run appears grounded in intermediate work.

### Expected Output

After the final report, the CLI prints a summary similar to:

```text
=== Verification Summary ===
Run ID: run_...
Verification status: pass
Audit root node: workspace_...
Research root node: workspace_...
- [pass] tool_loop_used_multiple_steps: ...
- [pass] intermediate_memories_saved: ...
```

The verification summary is useful for checking that the report was not produced as a single-shot response.

### Inspect The Stored Hierarchy

To inspect the resulting Redis tree visually:

```bash
streamlit run apps/view_tree.py
```

In the Streamlit viewer, look for the run subtree created by the agent. Each run should include:

- a research root workspace
- audit workspaces such as `plan`, `execution`, `recalled_memories`, `report`, and `verification`
- intermediate saved memories under the research subtree

This makes it easier to confirm that the final report was assembled from prior stored work.
