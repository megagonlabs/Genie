"""Deferred memory formation, and the debounce that keeps it from thrashing.

LangMem can form memories on the hot path, while the agent is answering, or in
the background afterwards. The background path is what `ReflectionExecutor`
runs, and its interesting behavior is the delay: work is submitted with an
`after_seconds` window, and if another turn arrives on the same thread before
that window closes, the pending task is cancelled and rescheduled with the new
messages folded in. A three-turn exchange therefore produces one reflection over
the whole exchange rather than three over fragments of it.

Two Genie facades anchored on the same tier carry that behavior. `EventLog` is
the order submissions arrived; `TemporalCollection` holds each submission's due
time as its event time, so "what is ready to run?" is a bounded time query and
not a scan the lamp writes. Cancelling is dropping the temporal entry — the log
entry stays, so a superseded submission remains readable with the id of the one
that replaced it. Deferred work that never ran is usually the hardest thing to
see in a memory system; here it is the same read as everything else.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, cast

from genie import MemoryBlock, MemoryHandle
from lamps.langmem.manager import MemoryManager
from lamps.langmem.model import (
    SEARCH_LIMIT,
    SUBMISSION_TYPE,
    Conversation,
    ManagerRun,
    Message,
    Namespace,
    Patch,
    Submission,
    SubmissionState,
    format_timestamp,
    json_mapping,
    messages_from_json,
    namespace,
    normalize_timestamp,
    parse_timestamp,
    patch_as_json,
    patch_from_json,
    required_text,
)
from lamps.langmem.structures import LangMemStructures


class ReflectionQueue:
    """Queued conversations, their due times, and the debounce between them."""

    def __init__(
        self,
        memory: MemoryHandle,
        *,
        structures: LangMemStructures,
        manager: MemoryManager,
    ) -> None:
        """Create a queue over one bounded handle.

        Args:
            memory: Bounded Genie handle owning the queue tier.
            structures: Facade bundle built over the same handle.
            manager: Manager run against each submission that comes due.
        """

        self.memory = memory
        self.structures = structures
        self.manager = manager

    def submit(
        self,
        conversation: Conversation,
        *,
        base: Sequence[str],
        at: datetime,
        after_seconds: int = 0,
    ) -> Submission:
        """Queue a conversation, superseding any pending one on its thread.

        Args:
            conversation: Exchange and the changes proposed from it.
            base: Resolved namespace its collections sit under.
            at: When the conversation was handed over.
            after_seconds: Delay before the manager may run it. Zero makes the
                submission due immediately.

        Returns:
            The queued submission, carrying anything folded in from a
            submission it replaced.

        Raises:
            ValueError: If the delay is negative or the thread name is empty.
        """

        submitted_at = normalize_timestamp(at, field="at")
        delay = _delay(after_seconds)
        thread = required_text(conversation.thread, field="thread")
        target = namespace(*base)
        superseded = self._pending_for_thread(thread)
        messages, patches, profile = _fold(superseded, conversation)
        block = self.structures.tiers.create_child(
            parent=self.structures.queue_root,
            type=SUBMISSION_TYPE,
            value={
                "thread": thread,
                "namespace": list(target),
                "state": "pending",
                "submitted_at": format_timestamp(submitted_at),
                "due_at": format_timestamp(submitted_at + delay),
                "messages": [message.as_json() for message in messages],
                "patches": [patch_as_json(patch) for patch in patches],
                "profile": profile,
                "superseded_by": None,
            },
            metadata={"thread": thread, "state": "pending"},
        )
        entry = self.structures.queue.append(block)
        self.structures.schedule.add(block, occurred_at=submitted_at + delay)
        if superseded is not None:
            self._cancel(superseded, replaced_by=block.id)
        return _submission(entry.entry.sequence, block)

    def pending(self) -> list[Submission]:
        """Return the submissions still waiting to run, earliest due first.

        Returns:
            Pending submissions in due order.
        """

        return self._scheduled(stop=None)

    def due(self, now: datetime) -> list[Submission]:
        """Return the submissions whose delay has elapsed.

        Args:
            now: Current time.

        Returns:
            Due submissions in due order.
        """

        return self._scheduled(stop=_inclusive(normalize_timestamp(now, field="now")))

    def run_due(self, *, now: datetime) -> list[ManagerRun]:
        """Run the manager over every submission that has come due.

        Args:
            now: Current time. Submissions still inside their delay window are
                left on the queue.

        Returns:
            One run per processed submission, in due order.
        """

        moment = normalize_timestamp(now, field="now")
        runs: list[ManagerRun] = []
        for submission in self.due(moment):
            run = self.manager.apply(
                Conversation(
                    thread=submission.thread,
                    messages=submission.messages,
                    patches=submission.patches,
                    profile=submission.profile,
                ),
                base=self._namespace_of(submission),
                source=submission.block,
                at=moment,
            )
            self.memory.update(
                submission.block,
                value={"state": "processed", "processed_at": format_timestamp(moment)},
                metadata={"state": "processed"},
            )
            self.structures.schedule.remove(submission.block)
            runs.append(run)
        return runs

    def history(self) -> list[Submission]:
        """Return every submission ever queued, in arrival order.

        A cancelled submission is still here, marked `superseded` and naming
        the submission that replaced it, because the queue log is append-only.

        Returns:
            Submissions in ascending log order.
        """

        return [
            _submission(item.entry.sequence, item.block)
            for item in self.structures.queue.read(limit=SEARCH_LIMIT)
        ]

    def _pending_for_thread(self, thread: str) -> Submission | None:
        """Return the submission still waiting on one thread, if any."""

        return next(
            (submission for submission in self.pending() if submission.thread == thread),
            None,
        )

    def _cancel(self, submission: Submission, *, replaced_by: str) -> None:
        """Mark a submission superseded and take it off the schedule."""

        self.memory.update(
            submission.block,
            value={"state": "superseded", "superseded_by": replaced_by},
            metadata={"state": "superseded"},
        )
        self.structures.schedule.remove(submission.block)

    def _scheduled(self, *, stop: datetime | None) -> list[Submission]:
        """Return the scheduled submissions due before `stop`, in due order.

        The temporal read gives the due order and the log read gives each
        submission its sequence, so the two facades are consulted once each
        rather than once per submission.
        """

        sequences = {item.block.id: item.entry.sequence for item in self.structures.queue.read(limit=SEARCH_LIMIT)}
        scheduled: list[Submission] = []
        for item in self.structures.schedule.events(stop=stop, limit=SEARCH_LIMIT):
            sequence = sequences.get(item.block.id)
            if sequence is None:
                raise KeyError(f"Submission {item.block.id!r} is not on the queue log.")
            scheduled.append(_submission(sequence, item.block))
        return scheduled

    def _namespace_of(self, submission: Submission) -> Namespace:
        """Return the namespace a submission's collections sit under."""

        stored = _payload(submission.block).get("namespace")
        if not isinstance(stored, list):
            raise TypeError(f"Submission {submission.block.id!r} has an invalid namespace.")
        return namespace(*cast(list[str], stored))


def _fold(
    superseded: Submission | None,
    conversation: Conversation,
) -> tuple[tuple[Message, ...], tuple[Patch, ...], dict[str, Any]]:
    """Fold a cancelled submission's work into the one replacing it.

    Messages accumulate so the manager sees the whole exchange. A proposed
    change is replaced when the newer conversation speaks to the same address,
    because the fresher proposal is the one worth applying, and profile fields
    merge the same way.
    """

    if superseded is None:
        return conversation.messages, conversation.patches, deepcopy(conversation.profile)
    replaced = {(patch.collection, patch.key) for patch in conversation.patches}
    carried = tuple(patch for patch in superseded.patches if (patch.collection, patch.key) not in replaced)
    return (
        (*superseded.messages, *conversation.messages),
        (*carried, *conversation.patches),
        {**superseded.profile, **conversation.profile},
    )


def _submission(sequence: int, block: MemoryBlock) -> Submission:
    """Decode one queued submission."""

    payload = _payload(block)
    return Submission(
        block=block,
        sequence=sequence,
        thread=required_text(payload.get("thread"), field="thread"),
        state=_state(payload.get("state")),
        submitted_at=parse_timestamp(required_text(payload.get("submitted_at"), field="submitted_at")),
        due_at=parse_timestamp(required_text(payload.get("due_at"), field="due_at")),
        messages=messages_from_json(payload.get("messages", ())),
        patches=tuple(patch_from_json(entry) for entry in payload.get("patches", ())),
        profile=json_mapping(payload.get("profile"), field="profile"),
        superseded_by=cast(str | None, payload.get("superseded_by")),
    )


def _payload(block: MemoryBlock) -> dict[str, Any]:
    """Return the dictionary value stored on a submission block."""

    if block.type != SUBMISSION_TYPE or not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} is not a queued submission.")
    return cast(dict[str, Any], block.value)


def _state(value: object) -> SubmissionState:
    """Validate and narrow a stored submission state."""

    if value not in {"pending", "superseded", "processed"}:
        raise ValueError("A submission state must be 'pending', 'superseded', or 'processed'.")
    return cast(SubmissionState, value)


def _delay(after_seconds: object) -> timedelta:
    """Return the debounce window as a duration."""

    if isinstance(after_seconds, bool) or not isinstance(after_seconds, int) or after_seconds < 0:
        raise ValueError("after_seconds must be an integer of at least 0.")
    return timedelta(seconds=after_seconds)


def _inclusive(moment: datetime) -> datetime | None:
    """Return the exclusive bound that makes a due time at `moment` count.

    Temporal ranges are half-open, so a submission due exactly now needs the
    stop bound to sit just past it.
    """

    try:
        return moment + timedelta(microseconds=1)
    except OverflowError:
        return None
