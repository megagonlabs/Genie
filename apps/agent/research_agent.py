"""Reusable deep-research agent workflow built on top of Genie memory tools."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from genie.core.orchestrator import MemoryOrchestrator

from apps.agent.executor import ToolLoopExecutor
from apps.agent.prompts import (
    EXECUTION_PROMPT,
    PLANNING_PROMPT,
    SYNTHESIS_PROMPT,
    build_execution_user_prompt,
    build_planning_user_prompt,
    build_synthesis_user_prompt,
)
from apps.agent.tools import MEMORY_TOOLS, build_memory_tool_registry


class DeepResearchAgent:
    """Plan, execute, and synthesize long-running research with Genie memory.

    Parameters:
    openai_client:
        OpenAI client used for planning, execution, and synthesis.
    orchestrator:
        Genie orchestrator that exposes workspace, memory-save, and search tools.
    planning_model:
        Model used for the structured planning pass.
    execution_model:
        Model used for the long-running tool loop.
    synthesis_model:
        Model used to write the final report from recalled memories.
    max_tool_round_trips:
        Safety limit for the execution loop.
    """

    def __init__(
        self,
        openai_client: OpenAI,
        orchestrator: MemoryOrchestrator,
        planning_model: str = "gpt-4o",
        execution_model: str = "gpt-4o",
        synthesis_model: str = "gpt-4o",
        max_tool_round_trips: int = 30,
    ) -> None:
        self.client = openai_client
        self.orchestrator = orchestrator
        self.planning_model = planning_model
        self.synthesis_model = synthesis_model
        self.tool_executor = ToolLoopExecutor(
            openai_client=openai_client,
            model=execution_model,
            tools=MEMORY_TOOLS,
            available_functions=build_memory_tool_registry(orchestrator),
            max_round_trips=max_tool_round_trips,
        )

    def run(self, user_request: str) -> str:
        """Execute the full deep-research workflow.

        Parameters:
        user_request:
            The large research task to investigate and synthesize.

        Returns:
        str
            Final comprehensive report.
        """
        plan = self._build_plan(user_request)
        serialized_plan = json.dumps(plan, indent=2)
        execution_summary = self._execute_plan(user_request, serialized_plan)
        recalled_memories = self._recall_memories(plan)
        return self._synthesize_report(
            user_request=user_request,
            serialized_plan=serialized_plan,
            execution_summary=execution_summary,
            recalled_memories=json.dumps(recalled_memories, indent=2),
        )

    def _build_plan(self, user_request: str) -> Dict[str, Any]:
        """Generate a structured research plan from the user request."""
        messages: List[ChatCompletionMessageParam] = [
            {"role": "system", "content": PLANNING_PROMPT},
            {"role": "user", "content": build_planning_user_prompt(user_request)},
        ]
        response = self.client.chat.completions.create(
            model=self.planning_model,
            messages=messages,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)

    def _execute_plan(self, user_request: str, serialized_plan: str) -> str:
        """Run the long-lived tool loop that populates hierarchical memory."""
        messages: List[ChatCompletionMessageParam] = [
            {"role": "system", "content": EXECUTION_PROMPT},
            {
                "role": "user",
                "content": build_execution_user_prompt(user_request, serialized_plan),
            },
        ]
        return self.tool_executor.run(messages)

    def _recall_memories(self, plan: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Recall final supporting memories using the plan's synthesis queries."""
        queries = plan.get("final_search_queries", [])
        if not isinstance(queries, list):
            return []

        recalled_results: List[Dict[str, Any]] = []
        for query in queries:
            if not isinstance(query, str) or not query.strip():
                continue
            recalled_results.append(
                {
                    "query": query,
                    "results": self.orchestrator.search_memory(query_text=query, top_k=8),
                }
            )
        return recalled_results

    def _synthesize_report(
        self,
        user_request: str,
        serialized_plan: str,
        execution_summary: str,
        recalled_memories: str,
    ) -> str:
        """Write the final report from the plan, execution summary, and recalled memories."""
        messages: List[ChatCompletionMessageParam] = [
            {"role": "system", "content": SYNTHESIS_PROMPT},
            {
                "role": "user",
                "content": build_synthesis_user_prompt(
                    user_request=user_request,
                    serialized_plan=serialized_plan,
                    execution_summary=execution_summary,
                    recalled_memories=recalled_memories,
                ),
            },
        ]
        response = self.client.chat.completions.create(
            model=self.synthesis_model,
            messages=messages,
        )
        return response.choices[0].message.content or "Task completed with no text output."
