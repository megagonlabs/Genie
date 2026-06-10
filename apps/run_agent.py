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

# 1. Load the custom env file BEFORE initializing the OpenAI client
load_dotenv(PROJECT_ROOT / "openai.env")

# Map user choices to the prompts
AVAILABLE_PERSONAS = {"1": ("Deep Research Agent", DEEP_RESEARCH_PROMPT)}


def main():
    print("\n=========================================")
    print("        🧞 GENIE AI AGENT SYSTEM        ")
    print("=========================================")
    print("Select your Agent Persona:")

    for key, (name, _) in AVAILABLE_PERSONAS.items():
        print(f"  [{key}] {name}")

    choice = input("\nEnter choice: ").strip()

    # Default to Deep Research if they type something invalid
    if choice not in AVAILABLE_PERSONAS:
        print("Invalid choice. Defaulting to Deep Research Agent.")
        choice = "1"

    persona_name, system_prompt = AVAILABLE_PERSONAS[choice]
    print(f"\n✅ {persona_name} loaded.")

    # Interactive query prompt
    user_query = input(f"\nWhat would you like the {persona_name} to do?\n> ").strip()

    if not user_query:
        print("Query cannot be empty. Exiting.")
        sys.exit(1)

    print("\n⚙️  Initializing Memory Infrastructure...")

    try:
        # 2. Initialize the Memory Infrastructure
        redis_client = redis.Redis.from_url("redis://localhost:6379/0")
        redis_client.ping()  # Ping to ensure the database is actually running
    except redis.ConnectionError:
        print("\n❌ CRITICAL ERROR: Redis is not running.")
        print("Please ensure your local Redis Stack is running on port 6379.")
        sys.exit(1)

    orchestrator = MemoryOrchestrator(json_source=RedisJsonSource(redis_client), vector_source=RedisVectorSource(redis_client), index_manager=RedisIndexManager(redis_client))

    # Run infrastructure setup safely
    orchestrator.setup()

    # 3. Initialize OpenAI Client
    # (It automatically looks for OPENAI_API_KEY in the environment we just loaded)
    openai_client = OpenAI()

    # 4. Spin up the Agent Engine
    agent = AgentExecutor(openai_client=openai_client, orchestrator=orchestrator, system_prompt=system_prompt)

    print(f"🚀 Launching {persona_name}...")
    final_report = agent.run(user_query)

    print("\n================ FINAL OUTPUT ================\n")
    print(final_report)
    print("\n==============================================\n")


if __name__ == "__main__":
    main()
