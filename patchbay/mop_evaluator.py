"""Adapter from pydantic-ai + Haiku to the MOP Evaluator protocol.

MOP itself is provider-agnostic — it takes an `evaluator` callable at
construction. This module wraps Anthropic Haiku via pydantic-ai and
returns a callable matching MOP's Evaluator signature.

The structured-output schema (`mop.types.EvalLLMResponse`) is defined
in mop, not here. We hand it to pydantic-ai for output decoding, then
call mop's `verdict_from_eval_response()` translator. That keeps the
LLM-response wire format owned by the protocol — any future adapter
(Gemma, GPT, local) plugs into the same schema.

Reads MOP_ANTHROPIC_API_KEY first (preferred — kept out of subprocess
env), falls back to ANTHROPIC_API_KEY for compat. Raises RuntimeError
on first call if neither is set.

The all-rules-in-one-prompt batching from the alignment doc is built in:
each call sends every active rule's name+guidance with the message and
asks for one structured verdict. One Haiku call per submit.
"""

from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable

from mop.rules import Rule
from mop.types import EvalLLMResponse, Verdict, verdict_from_eval_response

logger = logging.getLogger(__name__)

_HAIKU_MODEL = "claude-haiku-4-5-20251001"


def _build_eval_agent():
    """Construct a pydantic-ai Agent that decodes mop's EvalLLMResponse."""
    from pydantic_ai import Agent
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    api_key = os.environ.get("MOP_ANTHROPIC_API_KEY") or os.environ.get(
        "ANTHROPIC_API_KEY"
    )
    if not api_key:
        raise RuntimeError(
            "No API key for MOP haiku evaluator. Set MOP_ANTHROPIC_API_KEY "
            "(preferred — kept out of spawned subprocesses) or ANTHROPIC_API_KEY."
        )
    model = AnthropicModel(_HAIKU_MODEL, provider=AnthropicProvider(api_key=api_key))
    return Agent(model, output_type=EvalLLMResponse)


def build_haiku_evaluator(
    *, rules: list[Rule]
) -> Callable[[str, list[str], str | None], Awaitable[Verdict]]:
    """Return an Evaluator-shaped callable wrapping Haiku via pydantic-ai."""
    _agent = None  # lazy — built on first call so missing API key fails late, not at import

    async def evaluate(
        text: str, regex_hints: list[str], justification: str | None
    ) -> Verdict:
        nonlocal _agent
        if _agent is None:
            _agent = _build_eval_agent()

        rule_lines = "\n".join(
            f"  - {r.name}: {(r.guidance or '').strip()}" for r in rules
        ) or "  (no active rules)"
        hint_line = (
            f"Regex prelim hits (advisory, not authoritative): {', '.join(regex_hints)}"
            if regex_hints
            else "Regex prelim: clean."
        )
        just_line = (
            f"\nThe agent has provided this justification for the message:\n"
            f"  {justification}\n"
            "Decide whether the justification clears the rule violation."
            if justification
            else ""
        )
        query = (
            "You are a message gate. Active rules:\n"
            f"{rule_lines}\n\n"
            f"{hint_line}\n\n"
            "Message under review:\n"
            f"<message>\n{text}\n</message>\n"
            f"{just_line}\n\n"
            "Decide one of three actions:\n"
            "  - accept: message passes all rules, deliver as-is\n"
            "  - rewrite: message violates style but is fixable; provide the corrected text in 'rewritten'\n"
            "  - reject: message violates substantive rules; list the violated rule names in 'violations'\n"
        )
        result = await _agent.run(query)
        return verdict_from_eval_response(result.output, original_text=text)

    return evaluate
