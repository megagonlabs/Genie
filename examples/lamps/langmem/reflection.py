"""Deferred memory formation, and the debounce that stops repeated runs.

LangMem can form memories on the hot path, while the agent answers, or in the
background after the conversation. `ReflectionExecutor` runs the background
path. Its important behavior is the delay:

- **Window:** a caller submits work with an `after_seconds` window.
- **Debounce:** if another turn arrives on the same thread before that window
  closes, the executor cancels the pending task. Then it schedules the task
  again, with the new messages merged in.
- **Result:** a 3-turn exchange produces one reflection over the whole
  exchange, not 3 reflections over parts of it.

The lamp uses 2 Genie facades with their anchor on the same tier:

- **`EventLog`:** it keeps the arrival order of the submissions.
- **`TemporalCollection`:** it keeps the due time of each submission as its
  event time. So "what is ready to run?" is a bounded time query, not a scan
  that the lamp writes.
- **Cancel:** the queue removes the temporal entry. The log entry stays, so a
  superseded submission stays readable, with the id of the submission that
  replaced it. In a memory system, deferred work that never ran is usually the
  hardest thing to see. Here, a reader finds it with the same read as all other
  data.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, cast

from examples.lamps.langmem.manager import MemoryManager
from examples.lamps.langmem.model import (
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
from examples.lamps.langmem.structures import LangMemStructures
from genie import MemoryBlock, MemoryHandle


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
            memory: Bounded Genie handle that owns the queue tier.
            structures: Facade bundle on the same handle.
            manager: Manager that runs each submission when it is due.
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
        """Queue a conversation, and supersede the pending submission on its thread, if one exists.

        The method supersedes only a pending submission with the same thread
        name in the same namespace.

        Args:
            conversation: The exchange and its proposed changes.
            base: Resolved namespace that contains its collections.
            at: Time when the caller gave the conversation to the queue.
            after_seconds: Delay before the manager can run it. With 0, the
                submission is due immediately.

        Returns:
            The queued submission. It includes the work that the queue merged
            from the submission that it replaced.

        Raises:
            ValueError: If the delay is not an integer of 0 or more, or the
                thread name is empty.
        """

        submitted_at = normalize_timestamp(at, field="at")
        delay = _delay(after_seconds)
        thread = required_text(conversation.thread, field="thread")
        target = namespace(*base)
        superseded = self._pending_for_thread(thread, base=target)
        messages, patches, profile = _fold(superseded, conversation)
        block = self.structures.tiers.create_child(
            parent=self.structures.ensure_namespace(target),
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

    def pending(self, *, base: Sequence[str] | None = None) -> list[Submission]:
        """Return the submissions that did not run yet, earliest due time first.

        Args:
            base: Namespace to read. `None` reads all namespaces. A background
                runner that owns the whole store uses `None`. A caller for one
                user gives the namespace of that user.

        Returns:
            Pending submissions in due order.
        """

        return self._scheduled(stop=None, base=base)

    def due(self, now: datetime, *, base: Sequence[str] | None = None) -> list[Submission]:
        """Return the submissions whose delay is over.

        Args:
            now: Current time. A submission that is due exactly at `now` is
                included.
            base: Namespace to read, as for `pending`.

        Returns:
            Due submissions in due order.
        """

        return self._scheduled(stop=_inclusive(normalize_timestamp(now, field="now")), base=base)

    def run_due(self, *, now: datetime, base: Sequence[str] | None = None) -> list[ManagerRun]:
        """Run the manager over each due submission.

        Args:
            now: Current time. Submissions that are still in their delay window
                stay on the queue.
            base: Namespace to run. `None` runs all namespaces. A background
                runner that owns the whole store uses `None`. A caller for one
                user gives the namespace of that user. Then it cannot run the
                submission of another user, and it cannot see its result.

        Returns:
            One run for each processed submission, in due order.
        """

        moment = normalize_timestamp(now, field="now")
        runs: list[ManagerRun] = []
        for submission in self.due(moment, base=base):
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
        """Return all submissions that the queue received, in arrival order.

        The queue log is append-only. So a cancelled submission is still in the
        list. Its state is `superseded`, and it has the id of the submission
        that replaced it.

        Returns:
            Submissions in ascending log order.
        """

        return [
            _submission(item.entry.sequence, item.block)
            for item in self.structures.queue.read(limit=SEARCH_LIMIT)
        ]

    def _pending_for_thread(self, thread: str, *, base: Namespace) -> Submission | None:
        """Return the pending submission on one thread, or `None`.

        A thread name is unique only in one namespace. So the method matches
        the pair of namespace and thread. Without this, if 2 users chose the
        same thread name, each user would cancel the queued work of the other
        user and merge it into their own submission.
        """

        return next(
            (submission for submission in self.pending(base=base) if submission.thread == thread),
            None,
        )

    def _cancel(self, submission: Submission, *, replaced_by: str) -> None:
        """Mark a submission as `superseded`, and remove it from the schedule."""

        self.memory.update(
            submission.block,
            value={"state": "superseded", "superseded_by": replaced_by},
            metadata={"state": "superseded"},
        )
        self.structures.schedule.remove(submission.block)

    def _scheduled(self, *, stop: datetime | None, base: Sequence[str] | None = None) -> list[Submission]:
        """Return the scheduled submissions that are due before `stop`, in due order.

        - **2 reads:** the temporal read gives the due order. The log read gives
          the sequence of each submission. So the method reads each facade
          once, not once for each submission.
        - **Namespace filter:** one queue tier holds the submissions of all
          namespaces. So the method applies `base` here, after the temporal
          read. `TemporalCollection` orders by time and has no metadata
          filter. The namespace that each submission recorded narrows the
          result.
        """

        wanted = None if base is None else namespace(*base)
        sequences = {item.block.id: item.entry.sequence for item in self.structures.queue.read(limit=SEARCH_LIMIT)}
        scheduled: list[Submission] = []
        for item in self.structures.schedule.events(stop=stop, limit=SEARCH_LIMIT):
            sequence = sequences.get(item.block.id)
            if sequence is None:
                raise KeyError(f"Submission {item.block.id!r} is not on the queue log.")
            submission = _submission(sequence, item.block)
            if wanted is not None and self._namespace_of(submission) != wanted:
                continue
            scheduled.append(submission)
        return scheduled

    def _namespace_of(self, submission: Submission) -> Namespace:
        """Return the namespace that contains the collections of a submission."""

        stored = _payload(submission.block).get("namespace")
        if not isinstance(stored, list):
            raise TypeError(f"Submission {submission.block.id!r} has an invalid namespace.")
        return namespace(*cast(list[str], stored))


def _fold(
    superseded: Submission | None,
    conversation: Conversation,
) -> tuple[tuple[Message, ...], tuple[Patch, ...], dict[str, Any]]:
    """Merge the work of a cancelled submission into the submission that replaces it.

    - **Messages:** they accumulate, so the manager sees the whole exchange.
    - **Proposed changes:** if the newer conversation has a change for the same
      address, its change replaces the older change. The newer proposal is the
      one to apply.
    - **Profile fields:** they merge the same way. The newer value replaces the
      older value.
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
    """Return the dictionary value of a submission block."""

    if block.type != SUBMISSION_TYPE or not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} is not a queued submission.")
    return cast(dict[str, Any], block.value)


def _state(value: object) -> SubmissionState:
    """Validate a stored submission state, and narrow its type."""

    if value not in {"pending", "superseded", "processed"}:
        raise ValueError("A submission state must be 'pending', 'superseded', or 'processed'.")
    return cast(SubmissionState, value)


def _delay(after_seconds: object) -> timedelta:
    """Return the debounce window as a duration."""

    if isinstance(after_seconds, bool) or not isinstance(after_seconds, int) or after_seconds < 0:
        raise ValueError("after_seconds must be an integer of at least 0.")
    return timedelta(seconds=after_seconds)


def _inclusive(moment: datetime) -> datetime | None:
    """Return the exclusive stop bound that includes a due time at `moment`.

    Temporal ranges are half-open. So for a submission that is due exactly at
    `moment`, the stop bound is 1 microsecond after `moment`. If that
    calculation overflows, the function returns `None`, which means no stop
    bound.
    """

    try:
        return moment + timedelta(microseconds=1)
    except OverflowError:
        return None
