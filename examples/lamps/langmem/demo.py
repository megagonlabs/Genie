"""Run a code-review agent with LangMem-inspired memory tools.

From the repository root:

    python -m examples.lamps.langmem.demo
"""

from __future__ import annotations

from examples.lamps.demo_output import print_agent_demo
from examples.lamps.langmem.agent import LangMemAgent, LangMemTools
from examples.lamps.langmem.manager import MemoryManager
from examples.lamps.langmem.model import (
    STORE_TYPE,
    AnnotatedTrajectory,
    Conversation,
    Message,
    Patch,
    PromptRule,
    ValueSchema,
    parse_timestamp,
)
from examples.lamps.langmem.profiles import ProfileStore
from examples.lamps.langmem.prompts import PromptOptimizer
from examples.lamps.langmem.reflection import ReflectionQueue
from examples.lamps.langmem.store import MemoryStore
from examples.lamps.langmem.structures import LangMemStructures
from genie import InMemoryBackend, Memory

NAMESPACE_TEMPLATE = ("memories", "{user_id}")
CONTEXT = {"user_id": "dana"}
NAMESPACE = ("memories", "dana")
PROFILE_NAME = "preferences"
PROMPT_NAME = "code-review"

OPENED_AT = "2025-05-05T08:00:00Z"
FIRST_TURN_AT = "2025-05-05T09:00:00Z"
SECOND_TURN_AT = "2025-05-05T09:04:00Z"
FIRST_REFLECTION_AT = "2025-05-05T09:20:00Z"
THIRD_TURN_AT = "2025-05-05T11:00:00Z"
OPTIMIZED_AT = "2025-05-05T12:00:00Z"

DEBOUNCE_SECONDS = 600

SCHEMAS: dict[tuple[str, ...], ValueSchema] = {
    (*NAMESPACE, "semantic"): ValueSchema(
        "semantic_fact",
        {"subject": str, "predicate": str, "object": str, "context": str},
    ),
    (*NAMESPACE, "episodes"): ValueSchema(
        "episode",
        {"observation": str, "thoughts": str, "action": str, "result": str},
    ),
}

PROFILE_SCHEMA = ValueSchema(
    "user_profile",
    {"name": str, "language": str, "review_tone": str},
)

PROFILE_DEFAULT = {"name": "Dana", "language": "python", "review_tone": "neutral"}

BASE_PROMPT = "Review the diff and report what will break."
WHEN_TO_UPDATE = "when a reviewer says a comment was unhelpful or missing"

FIRST_TURNS = Conversation(
    thread="review-812",
    messages=(
        Message("user", "Here is the billing service diff. It swaps FastAPI for Litestar."),
        Message("assistant", "I will review it against your test suite."),
    ),
    patches=(
        Patch(
            action="insert",
            collection="semantic",
            key="stack",
            value={
                "subject": "billing service",
                "predicate": "runs on",
                "object": "FastAPI",
                "context": "before this diff",
            },
        ),
        Patch(
            action="insert",
            collection="semantic",
            key="tests",
            value={
                "subject": "Dana's team",
                "predicate": "tests with",
                "object": "pytest",
                "context": "every service",
            },
        ),
    ),
    profile={"review_tone": "direct"},
)

SECOND_TURNS = Conversation(
    thread="review-812",
    messages=(
        Message("user", "Correction: the swap already landed, and we dropped the nightly deploy window."),
        Message("assistant", "Noted. I will remember the current stack."),
    ),
    patches=(
        Patch(
            action="insert",
            collection="semantic",
            key="stack",
            value={
                "subject": "billing service",
                "predicate": "runs on",
                "object": "Litestar",
                "context": "after the swap landed",
            },
        ),
        Patch(
            action="insert",
            collection="semantic",
            key="deploy-window",
            value={
                "subject": "Dana's team",
                "predicate": "deploys",
                "object": "on demand",
                "context": "the nightly window was dropped",
            },
        ),
    ),
    profile={"name": "Dana Ruiz"},
)

THIRD_TURNS = Conversation(
    thread="review-830",
    messages=(
        Message("user", "The deploy-window note is stale. Carry it as a review rule instead."),
        Message("assistant", "I will drop the memory and keep the rule in the prompt."),
    ),
    patches=(
        Patch(action="delete", collection="semantic", key="deploy-window"),
        Patch(
            action="update",
            collection="semantic",
            key="tests",
            value={
                "subject": "Dana's team",
                "predicate": "tests with",
                "object": "pytest and a smoke suite",
                "context": "every service",
            },
        ),
    ),
)

EPISODE = {
    "observation": "The Litestar swap left the billing migration out of the diff.",
    "thoughts": "The last review missed a migration and the deploy failed.",
    "action": "Asked for the migration file before approving anything.",
    "result": "Dana added it and the deploy passed.",
}

TRAJECTORIES = (
    AnnotatedTrajectory(
        name="review-802-feedback",
        messages=(
            Message("assistant", "This handler shadows a name."),
            Message("user", "That is not what broke the build."),
        ),
        feedback="The comment never said which test would fail.",
        rules=(PromptRule("name-the-test", "Name the test that fails in every comment."),),
    ),
    AnnotatedTrajectory(
        name="review-812-feedback",
        messages=(
            Message("assistant", "Three spacing notes, then the missing migration."),
            Message("user", "The migration should have come first."),
        ),
        feedback="The review opened with nits before the real problem.",
        rules=(PromptRule("lead-with-breakage", "Lead with what breaks before any style comment."),),
    ),
    AnnotatedTrajectory(
        name="review-830-feedback",
        messages=(
            Message("assistant", "test_billing_migration will fail without the new file."),
            Message("user", "Good, that is what I needed."),
        ),
        feedback="The comment named the failing test, which is what the prompt already asks for.",
        rules=(PromptRule("name-the-test", "Name the test that fails in every comment."),),
    ),
)

QUESTION = "Should I approve the Litestar diff for Dana?"
QUERY = "billing service Litestar migration tests"


def build_agent() -> LangMemAgent:
    """Assemble an empty store, its five structures, and an agent over them.

    The tool set is bound to `("memories", "{user_id}")` with `user_id` supplied
    once, so nothing the agent passes later can name another user's namespace.
    The hot-path tool may create and update but not delete, while the background
    manager may delete, which is how the two write paths differ upstream.

    Returns:
        An agent whose only memory access is the named tool set.
    """

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="langmem-demo")
    root = memory.put(type=STORE_TYPE, value={"name": "Dana's review assistant"})
    handle = memory.get_handle(root=root)
    structures = LangMemStructures.create(handle, backend)
    store = MemoryStore(handle, structures=structures, schemas=SCHEMAS)
    profiles = ProfileStore(handle, structures=structures, schema=PROFILE_SCHEMA)
    profiles.register(
        NAMESPACE,
        PROFILE_NAME,
        default=PROFILE_DEFAULT,
        at=parse_timestamp(OPENED_AT),
    )
    optimizer = PromptOptimizer(handle, structures=structures)
    optimizer.register(PROMPT_NAME, prompt=BASE_PROMPT, when_to_update=WHEN_TO_UPDATE)
    manager = MemoryManager(
        store,
        profiles,
        profile_name=PROFILE_NAME,
        enable_deletes=True,
    )
    return LangMemAgent(
        name="review-agent",
        tools=LangMemTools(
            store,
            profiles=profiles,
            manager=manager,
            queue=ReflectionQueue(handle, structures=structures, manager=manager),
            optimizer=optimizer,
            namespace=NAMESPACE_TEMPLATE,
            context=CONTEXT,
            actions_permitted=("create", "update"),
        ),
    )


def teach_agent(agent: LangMemAgent) -> None:
    """Run both write paths and let feedback revise the agent's own prompt.

    The first two turns arrive four minutes apart on one thread, so the second
    supersedes the first inside its debounce window and one reflection covers
    the whole exchange. The agent then writes an episode on the hot path and is
    refused a delete there, before a later thread deletes the same memory
    through the manager.
    """

    agent.submit(FIRST_TURNS, at=FIRST_TURN_AT, after_seconds=DEBOUNCE_SECONDS)
    agent.submit(SECOND_TURNS, at=SECOND_TURN_AT, after_seconds=DEBOUNCE_SECONDS)
    agent.reflect(now=FIRST_REFLECTION_AT)
    agent.remember(action="create", collection="episodes", key="missed-migration", value=EPISODE)
    agent.remember(action="delete", collection="semantic", key="deploy-window")
    agent.submit(THIRD_TURNS, at=THIRD_TURN_AT)
    agent.reflect(now=THIRD_TURN_AT)
    for trajectory in TRAJECTORIES:
        agent.improve(prompt=PROMPT_NAME, trajectories=(trajectory,), at=OPTIMIZED_AT)


def answer_question(agent: LangMemAgent) -> str:
    """Have the agent answer from all four kinds of memory it keeps."""

    return agent.answer(
        QUESTION,
        prompt=PROMPT_NAME,
        query=QUERY,
        schema="semantic_fact",
        example_schema="episode",
    )


def main() -> None:
    """Run the complete deterministic LangMem-inspired agent workflow."""

    agent = build_agent()
    teach_agent(agent)
    answer = answer_question(agent)
    print_agent_demo(
        title="LangMem-inspired agent memory",
        purpose=(
            "An agent forms memories on the hot path and in a debounced "
            "background queue, keeps a single profile document current, and "
            "revises its own prompt from feedback."
        ),
        structures=(
            "ownership tree",
            "keyed collection",
            "two versioned records",
            "event log",
            "temporal collection",
            "lineage DAG",
        ),
        tools=agent.tools,
        answer=answer,
        calls=agent.call_log,
    )


if __name__ == "__main__":
    main()
