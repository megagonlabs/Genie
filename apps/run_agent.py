"""CLI entry point for the reusable deep-research Genie agent."""

from __future__ import annotations

import sys
from pathlib import Path

import redis
from dotenv import load_dotenv
from openai import OpenAI

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from genie import MemoryOrchestrator
from genie.infrastructure.redis_index_manager import RedisIndexManager
from genie.infrastructure.redis_json_source import RedisJsonSource
from genie.infrastructure.redis_vector_source import RedisVectorSource

from apps.agent import DeepResearchAgent

load_dotenv(PROJECT_ROOT / "openai.env")


def build_orchestrator(redis_url: str = "redis://localhost:6379/0") -> MemoryOrchestrator:
    """Construct a provisioned Genie orchestrator for the CLI agent.

    Parameters:
    redis_url:
        Redis connection URL for the local memory backend.

    Returns:
    MemoryOrchestrator
        Ready-to-use orchestrator instance.
    """
    redis_client = redis.Redis.from_url(redis_url)
    redis_client.ping()
    orchestrator = MemoryOrchestrator(
        json_source=RedisJsonSource(redis_client),
        vector_source=RedisVectorSource(redis_client),
        index_manager=RedisIndexManager(redis_client),
    )
    orchestrator.provision()
    return orchestrator


def build_agent(orchestrator: MemoryOrchestrator) -> DeepResearchAgent:
    """Construct the reusable deep-research agent used by the CLI."""
    return DeepResearchAgent(
        openai_client=OpenAI(),
        orchestrator=orchestrator,
    )


def main() -> None:
    """Run the interactive deep-research CLI."""
    user_query = input("Research topic: ").strip()
    if not user_query:
        sys.exit(1)

    try:
        orchestrator = build_orchestrator()
    except redis.ConnectionError:
        sys.exit(1)

    agent = build_agent(orchestrator)
    final_report = agent.run(user_query)
    print(final_report)


if __name__ == "__main__":
    main()
