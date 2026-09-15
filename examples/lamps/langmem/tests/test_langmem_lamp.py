"""Behavioral evidence for the LangMem-inspired Genie lamp."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from examples.lamps.langmem.agent import LangMemTools
from examples.lamps.langmem.demo import answer_question, build_agent, main, teach_agent
from examples.lamps.langmem.manager import MemoryManager
from examples.lamps.langmem.model import (
    ITEM_TYPE,
    AnnotatedTrajectory,
    Conversation,
    Message,
    Patch,
    PromptRule,
    ValueSchema,
    resolve_namespace,
)
from examples.lamps.langmem.profiles import ProfileStore
from examples.lamps.langmem.prompts import PromptOptimizer
from examples.lamps.langmem.reflection import ReflectionQueue
from examples.lamps.langmem.store import MemoryStore
from examples.lamps.langmem.structures import LangMemStructures
from genie import InMemoryBackend, Memory, MemoryHandle
from genie.structures import MemoryAddress

TEMPLATE = ("memories", "{user_id}")
DANA = ("memories", "dana")
BOB = ("memories", "bob")

OPENED = datetime(2025, 5, 5, 8, tzinfo=UTC)
NOON = datetime(2025, 5, 5, 12, tzinfo=UTC)

FACT_SCHEMA = ValueSchema(
    "semantic_fact",
    {"subject": str, "predicate": str, "object": str, "context": str},
)
EPISODE_SCHEMA = ValueSchema(
    "episode",
    {"observation": str, "thoughts": str, "action": str, "result": str},
)
PROFILE_SCHEMA = ValueSchema("user_profile", {"name": str, "language": str, "review_tone": str})
PROFILE_DEFAULT = {"name": "Dana", "language": "python", "review_tone": "neutral"}


@dataclass(frozen=True, slots=True)
class Lamp:
    """The wired pieces one test works against."""

    handle: MemoryHandle
    structures: LangMemStructures
    store: MemoryStore
    profiles: ProfileStore
    manager: MemoryManager
    queue: ReflectionQueue
    optimizer: PromptOptimizer


def _lamp(**permissions: bool) -> Lamp:
    """Build an empty lamp with a registered profile and prompt."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="langmem-test")
    root = memory.put(type="langmem.store", value={"name": "test"})
    handle = memory.get_handle(root=root)
    structures = LangMemStructures.create(handle, backend)
    schemas: dict[tuple[str, ...], ValueSchema] = {
        (*DANA, "semantic"): FACT_SCHEMA,
        (*DANA, "episodes"): EPISODE_SCHEMA,
        (*BOB, "semantic"): FACT_SCHEMA,
    }
    store = MemoryStore(handle, structures=structures, schemas=schemas)
    profiles = ProfileStore(handle, structures=structures, schema=PROFILE_SCHEMA)
    profiles.register(DANA, "preferences", default=PROFILE_DEFAULT, at=OPENED)
    optimizer = PromptOptimizer(handle, structures=structures)
    optimizer.register("code-review", prompt="Review the diff.", when_to_update="on reviewer feedback")
    manager = MemoryManager(store, profiles, profile_name="preferences", **permissions)
    return Lamp(
        handle=handle,
        structures=structures,
        store=store,
        profiles=profiles,
        manager=manager,
        queue=ReflectionQueue(handle, structures=structures, manager=manager),
        optimizer=optimizer,
    )


def _tools(lamp: Lamp, *, user: str = "dana", actions: tuple[str, ...] = ("create", "update", "delete")) -> LangMemTools:
    """Bind a tool set to one user's namespace."""

    return LangMemTools(
        lamp.store,
        profiles=lamp.profiles,
        manager=lamp.manager,
        queue=lamp.queue,
        optimizer=lamp.optimizer,
        namespace=TEMPLATE,
        context={"user_id": user},
        actions_permitted=actions,
    )


def _fact(subject: str, predicate: str, object_: str, context: str) -> dict[str, str]:
    return {"subject": subject, "predicate": predicate, "object": object_, "context": context}


def _episode(observation: str, result: str) -> dict[str, str]:
    return {
        "observation": observation,
        "thoughts": "Apply what the profile already says.",
        "action": "Asked for the missing file.",
        "result": result,
    }


def test_namespace_template_resolves_once_so_a_tool_set_cannot_leave_its_user() -> None:
    lamp = _lamp()
    dana = _tools(lamp)
    bob = _tools(lamp, user="bob")
    dana.call(
        "manage_memory",
        {
            "action": "create",
            "collection": "semantic",
            "key": "stack",
            "value": _fact("billing service", "runs on", "Litestar", "after the swap"),
        },
    )

    assert dana.namespace == DANA
    assert bob.namespace == BOB
    assert resolve_namespace(TEMPLATE, {"user_id": "dana"}) == DANA
    assert bob.call("search_memory", {"query": "Litestar billing"})["results"] == []
    assert len(dana.call("search_memory", {"query": "Litestar billing"})["results"]) == 1
    with pytest.raises(KeyError, match="placeholder"):
        resolve_namespace(TEMPLATE, {})


def test_items_keep_the_namespace_key_value_shape_across_schemas() -> None:
    lamp = _lamp()
    fact = lamp.store.insert((*DANA, "semantic"), "stack", _fact("billing", "runs on", "Litestar", "now"))
    episode = lamp.store.insert((*DANA, "episodes"), "missed", _episode("A migration was missing.", "It passed."))

    assert fact.block.type == episode.block.type == ITEM_TYPE
    assert fact.canonical_value() == {
        "namespace": ["memories", "dana", "semantic"],
        "key": "stack",
        "value": _fact("billing", "runs on", "Litestar", "now"),
    }
    assert fact.schema_name == "semantic_fact"
    assert episode.schema_name == "episode"
    assert fact.address == "memories/dana/semantic/stack"
    assert lamp.structures.items.get(MemoryAddress(fact.namespace, fact.key)) == fact.block


def test_supplied_schema_rejects_missing_extra_and_wrong_type_fields() -> None:
    lamp = _lamp()
    target = (*DANA, "semantic")

    with pytest.raises(ValueError, match="missing fields"):
        lamp.store.insert(target, "partial", {"subject": "billing"})
    with pytest.raises(ValueError, match="does not define"):
        lamp.store.insert(target, "extra", {**_fact("a", "b", "c", "d"), "confidence": 1.0})
    with pytest.raises(TypeError, match="must be str"):
        lamp.store.insert(target, "wrong", {**_fact("a", "b", "c", "d"), "object": ["c"]})


def test_an_unregistered_namespace_accepts_a_different_value_shape() -> None:
    lamp = _lamp()

    item = lamp.store.insert((*DANA, "notes"), "reminder", "Ask about the migration file.")

    assert item.value == "Ask about the migration file."
    assert item.schema_name is None


def test_an_update_keeps_the_item_identity_and_counts_the_patch() -> None:
    lamp = _lamp()
    original = lamp.store.insert((*DANA, "semantic"), "stack", _fact("billing", "runs on", "FastAPI", "before"))

    updated = lamp.store.update((*DANA, "semantic"), "stack", _fact("billing", "runs on", "Litestar", "after"))

    assert updated.id == original.id
    assert updated.namespace == original.namespace
    assert updated.key == original.key
    assert updated.value["object"] == "Litestar"
    assert (original.version, updated.version) == (1, 2)


def test_a_freed_address_can_be_inserted_into_again() -> None:
    lamp = _lamp()
    target = (*DANA, "semantic")
    first = lamp.store.insert(target, "stack", _fact("billing", "runs on", "FastAPI", "before"))

    lamp.store.delete(target, "stack")
    second = lamp.store.insert(target, "stack", _fact("billing", "runs on", "Litestar", "after"))

    assert lamp.store.get(target, "stack") == second
    assert second.id != first.id
    with pytest.raises(ValueError, match="already exists"):
        lamp.store.insert(target, "stack", _fact("billing", "runs on", "Flask", "never"))


def test_search_scopes_to_the_namespace_prefix_and_filters_on_metadata() -> None:
    lamp = _lamp()
    fact = lamp.store.insert((*DANA, "semantic"), "stack", _fact("billing service", "runs on", "Litestar", "after"))
    episode = lamp.store.insert((*DANA, "episodes"), "missed", _episode("The billing migration was missing.", "It passed."))
    lamp.store.insert((*BOB, "semantic"), "stack", _fact("billing service", "runs on", "Django", "always"))

    facts = lamp.store.search(DANA, "billing Litestar", filters={"schema": "semantic_fact"})
    episodes = lamp.store.search(DANA, "billing migration", filters={"schema": "episode"})
    everything = lamp.store.search(("memories",), "billing")

    assert [hit.item.id for hit in facts] == [fact.id]
    assert facts[0].score == 1.0
    assert facts[0].matched == ("billing", "litestar")
    assert [hit.item.id for hit in episodes] == [episode.id]
    assert len(everything) == 3


def test_search_pages_through_its_results_with_offset_and_limit() -> None:
    lamp = _lamp()
    for index in range(3):
        lamp.store.insert(
            (*DANA, "semantic"),
            f"fact-{index}",
            _fact("billing service", "runs on", "Litestar", f"note {index}"),
        )

    first = lamp.store.search(DANA, "billing service", limit=2, offset=0)
    second = lamp.store.search(DANA, "billing service", limit=2, offset=2)

    assert [hit.item.key for hit in first] == ["fact-0", "fact-1"]
    assert [hit.item.key for hit in second] == ["fact-2"]
    with pytest.raises(ValueError, match="offset"):
        lamp.store.search(DANA, "billing", offset=-1)


def test_forgetting_a_namespace_deletes_its_items_and_leaves_a_sibling_alone() -> None:
    lamp = _lamp()
    dana = lamp.store.insert((*DANA, "semantic"), "stack", _fact("billing", "runs on", "Litestar", "after"))
    lamp.store.insert((*DANA, "episodes"), "missed", _episode("A migration was missing.", "It passed."))
    bob = lamp.store.insert((*BOB, "semantic"), "stack", _fact("billing", "runs on", "Django", "always"))

    removed = lamp.store.forget(DANA)

    assert [item.address for item in removed] == [
        "memories/dana/episodes/missed",
        "memories/dana/semantic/stack",
    ]
    assert lamp.store.items(DANA) == []
    assert lamp.structures.items.get(MemoryAddress(dana.namespace, dana.key)) is None
    assert lamp.store.namespace_node(DANA) is None
    assert lamp.store.get(BOB + ("semantic",), "stack") == bob
    with pytest.raises(KeyError, match="does not exist"):
        lamp.store.forget(DANA)


def test_the_profile_is_one_document_whose_earlier_versions_stay_readable() -> None:
    lamp = _lamp()

    first = lamp.profiles.current(DANA, "preferences")
    second = lamp.profiles.update(DANA, "preferences", {"review_tone": "direct"}, at=NOON)
    unchanged = lamp.profiles.update(DANA, "preferences", {"review_tone": "direct"}, at=NOON)
    history = lamp.profiles.history(DANA, "preferences")

    assert first is not None
    assert (first.revision, second.revision, unchanged.revision) == (1, 2, 2)
    assert first.value == PROFILE_DEFAULT
    assert second.value == {**PROFILE_DEFAULT, "review_tone": "direct"}
    assert [version.revision for version in history] == [1, 2]
    assert second.identity.id == first.identity.id
    assert lamp.profiles.profiles() == [second]


def test_a_profile_merge_that_breaks_its_schema_is_refused() -> None:
    lamp = _lamp()

    with pytest.raises(ValueError, match="does not define"):
        lamp.profiles.update(DANA, "preferences", {"timezone": "UTC"}, at=NOON)
    with pytest.raises(KeyError, match="not registered"):
        lamp.profiles.update(BOB, "preferences", {"review_tone": "direct"}, at=NOON)


def test_the_manager_refuses_the_kinds_of_change_it_is_not_enabled_for() -> None:
    lamp = _lamp(enable_deletes=False)
    lamp.store.insert((*DANA, "semantic"), "stale", _fact("billing", "deploys", "nightly", "old"))

    run = lamp.manager.apply(
        Conversation(
            thread="review-830",
            messages=(Message("user", "Drop the nightly note."),),
            patches=(Patch(action="delete", collection="semantic", key="stale"),),
        ),
        base=DANA,
        at=NOON,
    )

    assert lamp.manager.permissions == {"insert": True, "update": True, "delete": False}
    assert [outcome.reason for outcome in run.refused] == ["delete is not enabled for this manager"]
    assert run.applied == ()
    assert lamp.store.get((*DANA, "semantic"), "stale") is not None


def test_the_manager_refuses_a_profile_revision_it_is_not_enabled_for() -> None:
    lamp = _lamp(enable_updates=False)
    submission = lamp.queue.submit(
        Conversation(
            thread="review-831",
            messages=(Message("user", "Be direct with me."),),
            profile={"review_tone": "direct"},
        ),
        base=DANA,
        at=NOON,
    )

    run = lamp.queue.run_due(now=NOON)[0]
    profile = lamp.profiles.require(DANA, "preferences")

    assert run.profile_reason == "update is not enabled for this manager"
    assert run.profile is not None and run.profile.revision == 1
    assert profile.revision == 1
    assert profile.value["review_tone"] == "neutral"
    assert [version.revision for version in lamp.profiles.history(DANA, "preferences")] == [1]
    assert lamp.structures.lineage.successors(submission.block) == []


def test_a_manager_that_may_update_still_revises_the_profile() -> None:
    lamp = _lamp(enable_updates=True)

    run = lamp.manager.apply(
        Conversation(
            thread="review-832",
            messages=(Message("user", "Be direct with me."),),
            profile={"review_tone": "direct"},
        ),
        base=DANA,
        at=NOON,
    )

    assert run.profile_reason is None
    assert run.profile is not None and run.profile.revision == 2
    assert lamp.profiles.require(DANA, "preferences").value["review_tone"] == "direct"


def test_the_manager_turns_a_colliding_insert_into_a_patch_of_the_stored_memory() -> None:
    lamp = _lamp()
    stored = lamp.store.insert((*DANA, "semantic"), "stack", _fact("billing", "runs on", "FastAPI", "before"))

    run = lamp.manager.apply(
        Conversation(
            thread="review-812",
            messages=(Message("user", "The swap landed."),),
            patches=(
                Patch(
                    action="insert",
                    collection="semantic",
                    key="stack",
                    value=_fact("billing", "runs on", "Litestar", "after"),
                ),
                Patch(
                    action="update",
                    collection="semantic",
                    key="absent",
                    value=_fact("billing", "alerts", "PagerDuty", "always"),
                ),
            ),
        ),
        base=DANA,
        at=NOON,
    )

    reconciled, promoted = run.outcomes
    assert (reconciled.patch.action, reconciled.action) == ("insert", "update")
    assert reconciled.item is not None and reconciled.item.id == stored.id
    assert reconciled.item.version == 2
    assert (promoted.patch.action, promoted.action) == ("update", "insert")
    assert lamp.store.get((*DANA, "semantic"), "absent") is not None


def test_reflection_records_which_conversation_wrote_each_memory() -> None:
    lamp = _lamp()
    submission = lamp.queue.submit(
        Conversation(
            thread="review-812",
            messages=(Message("user", "We test with pytest."),),
            patches=(
                Patch(
                    action="insert",
                    collection="semantic",
                    key="tests",
                    value=_fact("Dana's team", "tests with", "pytest", "every service"),
                ),
            ),
            profile={"review_tone": "direct"},
        ),
        base=DANA,
        at=OPENED,
    )

    run = lamp.queue.run_due(now=OPENED)[0]
    item = lamp.store.require((*DANA, "semantic"), "tests")
    profile = lamp.profiles.require(DANA, "preferences")

    assert run.source_id == submission.block.id
    assert [neighbor.block.id for neighbor in lamp.structures.lineage.predecessors(item.block)] == [submission.block.id]
    assert [neighbor.block.id for neighbor in lamp.structures.lineage.predecessors(profile.block)] == [submission.block.id]
    assert [neighbor.link.metadata["role"] for neighbor in lamp.structures.lineage.successors(submission.block)] == [
        "extracted",
        "revised",
    ]


def test_a_second_turn_supersedes_the_pending_submission_and_folds_its_work() -> None:
    lamp = _lamp()
    first = lamp.queue.submit(
        Conversation(
            thread="review-812",
            messages=(Message("user", "It runs on FastAPI."),),
            patches=(
                Patch(action="insert", collection="semantic", key="stack", value=_fact("billing", "runs on", "FastAPI", "before")),
                Patch(action="insert", collection="semantic", key="tests", value=_fact("team", "tests with", "pytest", "always")),
            ),
            profile={"review_tone": "direct"},
        ),
        base=DANA,
        at=datetime(2025, 5, 5, 9, tzinfo=UTC),
        after_seconds=600,
    )
    second = lamp.queue.submit(
        Conversation(
            thread="review-812",
            messages=(Message("user", "Correction: Litestar."),),
            patches=(
                Patch(action="insert", collection="semantic", key="stack", value=_fact("billing", "runs on", "Litestar", "after")),
            ),
            profile={"name": "Dana Ruiz"},
        ),
        base=DANA,
        at=datetime(2025, 5, 5, 9, 4, tzinfo=UTC),
        after_seconds=600,
    )

    history = lamp.queue.history()
    runs = lamp.queue.run_due(now=datetime(2025, 5, 5, 9, 20, tzinfo=UTC))

    assert [submission.sequence for submission in history] == [0, 1]
    assert history[0].state == "superseded"
    assert history[0].superseded_by == second.block.id
    assert [message.content for message in second.messages] == [
        "It runs on FastAPI.",
        "Correction: Litestar.",
    ]
    assert [(patch.collection, patch.key) for patch in second.patches] == [
        ("semantic", "tests"),
        ("semantic", "stack"),
    ]
    assert second.profile == {"review_tone": "direct", "name": "Dana Ruiz"}
    assert len(runs) == 1
    assert lamp.store.require((*DANA, "semantic"), "stack").value["object"] == "Litestar"
    assert lamp.store.require((*DANA, "semantic"), "stack").version == 1
    assert first.block.id not in {run.source_id for run in runs}
    assert lamp.queue.pending() == []
    assert [submission.state for submission in lamp.queue.history()] == ["superseded", "processed"]


def test_a_submission_inside_its_delay_window_is_left_on_the_queue() -> None:
    lamp = _lamp()
    lamp.queue.submit(
        Conversation(
            thread="review-812",
            messages=(Message("user", "We test with pytest."),),
            patches=(Patch(action="insert", collection="semantic", key="tests", value=_fact("team", "tests with", "pytest", "always")),),
        ),
        base=DANA,
        at=datetime(2025, 5, 5, 9, tzinfo=UTC),
        after_seconds=600,
    )

    early = lamp.queue.run_due(now=datetime(2025, 5, 5, 9, 9, tzinfo=UTC))

    assert early == []
    assert [submission.state for submission in lamp.queue.pending()] == ["pending"]
    assert lamp.store.get((*DANA, "semantic"), "tests") is None

    on_time = lamp.queue.run_due(now=datetime(2025, 5, 5, 9, 10, tzinfo=UTC))

    assert len(on_time) == 1
    assert lamp.store.get((*DANA, "semantic"), "tests") is not None
    assert lamp.queue.pending() == []


def test_run_reflection_runs_only_the_queue_of_the_user_it_is_bound_to() -> None:
    lamp = _lamp()
    dana = _tools(lamp)
    bob = _tools(lamp, user="bob")
    dana.call(
        "submit_conversation",
        {
            "thread": "review-812",
            "messages": [{"role": "user", "content": "It runs on Litestar."}],
            "at": datetime(2025, 5, 5, 9, tzinfo=UTC).isoformat(),
            "patches": [
                {
                    "action": "insert",
                    "collection": "semantic",
                    "key": "stack",
                    "value": _fact("billing", "runs on", "Litestar", "after the swap"),
                }
            ],
        },
    )

    theirs = bob.call("run_reflection", {"now": NOON.isoformat()})

    assert theirs["runs"] == []
    assert theirs["still_pending"] == []
    assert lamp.store.get((*DANA, "semantic"), "stack") is None

    ours = dana.call("run_reflection", {"now": NOON.isoformat()})

    assert [run["thread"] for run in ours["runs"]] == ["review-812"]
    assert [outcome["address"] for outcome in ours["runs"][0]["applied"]] == ["semantic/stack"]
    assert lamp.store.require((*DANA, "semantic"), "stack").value["object"] == "Litestar"


def test_two_users_on_the_same_thread_name_do_not_supersede_each_other() -> None:
    lamp = _lamp()
    dana = _tools(lamp)
    bob = _tools(lamp, user="bob")
    dana.call(
        "submit_conversation",
        {
            "thread": "review-812",
            "messages": [{"role": "user", "content": "It runs on Litestar."}],
            "at": datetime(2025, 5, 5, 9, tzinfo=UTC).isoformat(),
            "after_seconds": 600,
            "patches": [
                {
                    "action": "insert",
                    "collection": "semantic",
                    "key": "stack",
                    "value": _fact("billing", "runs on", "Litestar", "after the swap"),
                }
            ],
        },
    )

    theirs = bob.call(
        "submit_conversation",
        {
            "thread": "review-812",
            "messages": [{"role": "user", "content": "Mine is a different exchange."}],
            "at": datetime(2025, 5, 5, 9, 4, tzinfo=UTC).isoformat(),
            "after_seconds": 600,
        },
    )

    assert theirs["superseded"] is None
    assert theirs["messages"] == 1
    assert theirs["patches"] == []
    assert [submission.state for submission in lamp.queue.history()] == ["pending", "pending"]

    ran = bob.call("run_reflection", {"now": NOON.isoformat()})

    assert [run["thread"] for run in ran["runs"]] == ["review-812"]
    assert ran["runs"][0]["applied"] == []
    assert lamp.store.get((*BOB, "semantic"), "stack") is None
    assert [submission.thread for submission in lamp.queue.pending(base=DANA)] == ["review-812"]


def test_a_prompt_revision_is_written_only_when_the_rules_change() -> None:
    lamp = _lamp()
    naming = AnnotatedTrajectory(
        name="review-802-feedback",
        messages=(Message("user", "It never said which test failed."),),
        feedback="The comment never named the failing test.",
        rules=(PromptRule("name-the-test", "Name the test that fails."),),
    )
    repeat = AnnotatedTrajectory(
        name="review-830-feedback",
        messages=(Message("user", "Good, it named the test."),),
        feedback="It named the failing test, as the prompt already asks.",
        rules=(PromptRule("name-the-test", "Name the test that fails."),),
    )

    base = lamp.optimizer.current("code-review")
    first = lamp.optimizer.optimize("code-review", (naming,), at=NOON)
    unchanged = lamp.optimizer.optimize("code-review", (repeat,), at=NOON)

    assert base is not None and (base.revision, base.text) == (0, "Review the diff.")
    assert base.when_to_update == "on reviewer feedback"
    assert first.revision == 1
    assert first.text == "Review the diff.\nRules learned from feedback:\n1. Name the test that fails."
    assert first.sources == ("review-802-feedback",)
    assert unchanged.revision == 1
    assert unchanged.sources == ("review-802-feedback",)
    assert lamp.optimizer.trajectory("review-830-feedback") is not None
    assert [version.revision for version in lamp.optimizer.history("code-review")] == [1]


def test_a_rule_with_a_known_key_replaces_its_earlier_wording_in_place() -> None:
    lamp = _lamp()
    first = AnnotatedTrajectory(
        name="round-one",
        messages=(Message("user", "Name the test."),),
        feedback="Name the failing test.",
        rules=(PromptRule("name-the-test", "Name the test that fails."),),
    )
    stronger = AnnotatedTrajectory(
        name="round-two",
        messages=(Message("user", "Name it in every comment."),),
        feedback="Every comment, not just the first.",
        rules=(PromptRule("name-the-test", "Name the failing test in every comment."),),
    )

    lamp.optimizer.optimize("code-review", (first,), at=NOON)
    revised = lamp.optimizer.optimize("code-review", (stronger,), at=NOON)

    assert revised.revision == 2
    assert [rule.key for rule in revised.rules] == ["name-the-test"]
    assert revised.rules[0].text == "Name the failing test in every comment."
    assert revised.sources == ("round-two",)
    assert lamp.optimizer.history("code-review")[0].rules[0].text == "Name the test that fails."
    assert lamp.optimizer.prompts() == [revised]


def test_the_hot_path_tool_refuses_an_action_it_was_not_given() -> None:
    lamp = _lamp(enable_deletes=True)
    tools = _tools(lamp, actions=("create", "update"))
    tools.call(
        "manage_memory",
        {
            "action": "create",
            "collection": "semantic",
            "key": "stale",
            "value": _fact("billing", "deploys", "nightly", "old"),
        },
    )

    refused = tools.call("manage_memory", {"action": "delete", "collection": "semantic", "key": "stale"})
    duplicate = tools.call(
        "manage_memory",
        {"action": "create", "collection": "semantic", "key": "stale", "value": _fact("a", "b", "c", "d")},
    )

    assert refused == {
        "action": "delete",
        "applied": False,
        "reason": "delete is not permitted for this tool",
        "address": "semantic/stale",
    }
    assert duplicate["applied"] is False
    assert duplicate["reason"] == "a memory is already stored at that address"
    assert lamp.store.get((*DANA, "semantic"), "stale") is not None
    with pytest.raises(ValueError, match="Unknown permitted actions"):
        _tools(lamp, actions=("create", "purge"))


def test_the_agent_uses_both_write_paths_and_all_four_kinds_of_memory(capsys) -> None:
    agent = build_agent()
    teach_agent(agent)

    answer = answer_question(agent)

    assert "Procedural memory (code-review r2, from review-812-feedback)" in answer
    assert "1. Name the test that fails in every comment." in answer
    assert "Profile preferences r2: language=python, name=Dana Ruiz, review_tone=direct" in answer
    assert "- was r1: language=python, name=Dana, review_tone=neutral" in answer
    assert "memories/dana/semantic/tests v2" in answer
    assert "memories/dana/episodes/missed-migration v1" in answer
    assert [call.name for call in agent.call_log] == [
        "submit_conversation",
        "submit_conversation",
        "run_reflection",
        "manage_memory",
        "manage_memory",
        "submit_conversation",
        "run_reflection",
        "optimize_prompt",
        "optimize_prompt",
        "optimize_prompt",
        "get_prompt",
        "get_profile",
        "search_memory",
        "search_memory",
    ]
    assert agent.tool_usage() == {
        "submit_conversation": 3,
        "run_reflection": 2,
        "manage_memory": 2,
        "optimize_prompt": 3,
        "get_prompt": 1,
        "get_profile": 1,
        "search_memory": 2,
    }
    assert capsys.readouterr().out == ""


def test_the_demo_debounces_one_thread_and_splits_deletes_across_the_two_paths() -> None:
    agent = build_agent()
    teach_agent(agent)
    calls = {index: record for index, record in enumerate(agent.call_log)}

    assert calls[1].result["superseded"] == {"sequence": 0, "thread": "review-812"}
    assert calls[1].result["messages"] == 4
    assert [outcome["address"] for outcome in calls[2].result["runs"][0]["applied"]] == [
        "semantic/tests",
        "semantic/stack",
        "semantic/deploy-window",
    ]
    assert calls[2].result["runs"][0]["profile"]["revision"] == 2
    assert calls[4].result == {
        "action": "delete",
        "applied": False,
        "reason": "delete is not permitted for this tool",
        "address": "semantic/deploy-window",
    }
    assert [outcome["address"] for outcome in calls[6].result["runs"][0]["applied"]] == [
        "semantic/deploy-window",
        "semantic/tests",
    ]
    assert calls[9].result["changed"] is False


def test_demo_uses_the_shared_terminal_format(capsys) -> None:
    main()

    output = capsys.readouterr().out
    assert output.startswith("== LangMem-inspired agent memory ==\n")
    assert "Genie structures: ownership tree, keyed collection, two versioned records" in output
    assert "\nTools available to the agent:\n" in output
    assert "\nAgent answer:\nQuestion:" in output
    assert "Inspectable tool calls:\n01. submit_conversation\n02. submit_conversation" in output
    assert "Tool usage:\n- submit_conversation x3\n- run_reflection x2" in output
