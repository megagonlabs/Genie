import sys
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from genie import MemoryOrchestrator
from genie.infrastructure.redis_json_source import RedisJsonSource
from genie.infrastructure.redis_vector_source import RedisVectorSource
from genie.infrastructure.redis_index_manager import RedisIndexManager
import redis
from apps.agent.executor import AgentExecutor
from apps.agent.prompts import DEEP_RESEARCH_PROMPT

load_dotenv(PROJECT_ROOT / "openai.env")
AVAILABLE_PERSONAS = {"1": ("Deep Research Agent", DEEP_RESEARCH_PROMPT)}


def main():
    for key, (name, _) in AVAILABLE_PERSONAS.items():
        print(f"[{key}] {name}")
    choice = input("\n").strip()
    if choice not in AVAILABLE_PERSONAS:
        choice = "1"
    persona_name, system_prompt = AVAILABLE_PERSONAS[choice]
    user_query = input(f"\n").strip()
    if not user_query:
        sys.exit(1)
    try:
        redis_client = redis.Redis.from_url("redis://localhost:6379/0")
        redis_client.ping()
    except redis.ConnectionError:
        sys.exit(1)
    orchestrator = MemoryOrchestrator(json_source=RedisJsonSource(redis_client), vector_source=RedisVectorSource(redis_client), index_manager=RedisIndexManager(redis_client))
    orchestrator.setup()
    openai_client = OpenAI()
    agent = AgentExecutor(openai_client=openai_client, orchestrator=orchestrator, system_prompt=system_prompt)
    final_report = agent.run(user_query)
    print(final_report)


if __name__ == "__main__":
    main()
