"""Confirmation of the state-changing operations on external targets (IMPLEMENTATION.md
§9; task M3).

A step that sends a state-changing operation to an external target is confirmed before
it is sent. When the client supports MCP elicitation, a person answers the question, and
the answer decides. When it does not, the operation goes ahead only if the target's
definition carries a reviewed opt-in for it; otherwise it is refused. Every decision is
journaled with how it was made.

Only a command is a state-changing step so far (`send_command`). A command to a managed
client needs its control API, which the scenario does not drive yet, so it stays
unsupported. A command to an external target is confirmed, and then unsupported: the
planner does not connect external targets yet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from mcp.types import (
    ClientCapabilities,
    ElicitationCapability,
    FormElicitationCapability,
)

from igs_iec104_mcp.models.bench import TestBench
from igs_iec104_mcp.models.target import ExternalTarget, Operation, undeclared
from igs_iec104_mcp.scenarios.engine import Context, StepFailed, StepUnsupported

CAPABILITY = ClientCapabilities(
    elicitation=ElicitationCapability(form=FormElicitationCapability())
)
# The question's answer: a single yes or no. Anything else is a refusal.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirm": {
            "type": "boolean",
            "title": "Send the operation",
            "description": "Answer yes only if the operation should reach the target.",
        }
    },
    "required": ["confirm"],
}


class Peer(Protocol):
    """The client's session as the confirmation sees it: its capabilities, and a form
    question to it."""

    def check_client_capability(self, capability: ClientCapabilities) -> bool: ...

    async def elicit_form(self, message: str, requested_schema: Any) -> Any: ...


class Confirmer(Protocol):
    """Asks a person whether an operation may be sent: the answer, and its words."""

    def supported(self) -> bool: ...

    async def ask(self, question: str) -> tuple[bool, str]: ...


class ElicitingConfirmer:
    """Asks through MCP elicitation, on the session of the client that started the run.
    Only an accepted answer with `confirm` set to true is a yes."""

    def __init__(self, peer: Peer) -> None:
        self._peer = peer

    def supported(self) -> bool:
        return bool(self._peer.check_client_capability(CAPABILITY))

    async def ask(self, question: str) -> tuple[bool, str]:
        try:
            result = await self._peer.elicit_form(question, SCHEMA)
        except Exception as error:  # a client that does not answer confirms nothing
            return False, f"the client did not answer ({type(error).__name__})"
        content = result.content or {}
        if result.action == "accept" and content.get("confirm") is True:
            return True, "confirmed by a person"
        return False, f"declined by a person ({result.action})"


@dataclass(frozen=True)
class Decision:
    """Whether an operation may be sent, how that was decided, and in what words."""

    accepted: bool
    how: str
    detail: str


async def decide(
    question: str,
    operation: Operation,
    target: ExternalTarget,
    confirmer: Confirmer | None,
) -> Decision:
    """The decision on one operation to an external target. A client that supports
    elicitation is asked; otherwise a reviewed opt-in of the target decides; otherwise
    the operation is refused."""
    if confirmer is not None and confirmer.supported():
        accepted, words = await confirmer.ask(question)
        return Decision(accepted, "elicitation", words)
    opt_in = target.opt_in
    if opt_in is not None and operation in opt_in.operations:
        return Decision(
            True,
            "definition",
            f"reviewed by {opt_in.reviewer} on {opt_in.reviewed_on}",
        )
    return Decision(
        False,
        "none",
        "the client cannot ask a person, and the target has no reviewed opt-in for "
        f"the {operation.value!r} operation",
    )


class Gate:
    """The handler of the `send_command` step for one run. It refuses a command to a
    target that does not allow it, asks for the confirmation of a command to an external
    target, and journals the decision through `record`."""

    def __init__(
        self,
        bench: TestBench,
        confirmer: Confirmer | None,
        record: Callable[[dict[str, Any]], None],
    ) -> None:
        self._targets = {target.name: target for target in bench.external_targets}
        self._confirmer = confirmer
        self._record = record

    async def send_command(self, step: Any, context: Context) -> str:
        target = self._targets.get(step.target)
        if target is None:
            raise StepUnsupported(
                f"a command to the managed instance {step.target!r} is not sent yet"
            )
        if undeclared(target, {Operation.COMMAND}):
            raise StepFailed(f"{step.target!r} does not allow the 'command' operation")
        question = (
            f"Send a {step.command} command with the value {step.value!r} to point "
            f"{step.address} of the external target {step.target!r}?"
        )
        decision = await decide(question, Operation.COMMAND, target, self._confirmer)
        self._record(
            {
                "operation": Operation.COMMAND.value,
                "target": step.target,
                "address": step.address,
                "accepted": decision.accepted,
                "how": decision.how,
                "detail": decision.detail,
            }
        )
        if not decision.accepted:
            raise StepFailed(
                f"the command to {step.target!r} was not confirmed: {decision.detail}"
            )
        raise StepUnsupported(
            f"the command to {step.target!r} was confirmed, but external connections "
            "are not carried out yet"
        )
