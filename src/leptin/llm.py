"""Merge / supersede decision and text fusion.

Default: ``HeuristicMerger`` — fuses two near-duplicate memories by unioning
their distinct sentences and detects contradictions via simple negation/antonym
and numeric-mismatch checks. Fully offline and deterministic.

Upgrade: ``HostedMerger`` uses a hosted LLM (Claude / GPT) for higher-quality
fusion and contradiction judgement when an API key is configured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z0-9']+")

# Lightweight antonym pairs for offline contradiction detection. Deliberately
# only domain-specific pairs — generic short words like on/off, yes/no,
# true/false, open/closed are excluded because they fire false-positive
# supersedes on unrelated text (e.g. "the light is on" vs "the door is off
# its hinges"). Conservative by design: "never silently forget".
_ANTONYMS = [
    {"likes", "dislikes"},
    {"likes", "hates"},
    {"loves", "hates"},
    {"prefers", "avoids"},
    {"enabled", "disabled"},
    {"allow", "deny"},
    {"increase", "decrease"},
    {"single", "married"},
    {"vegetarian", "omnivore"},
]


@dataclass
class MergeResult:
    action: str  # "merge" | "supersede"
    content: str  # canonical content (for merge); newer content (for supersede)
    reason: str


class Merger(Protocol):
    name: str

    def decide(self, older: str, newer: str, similarity: float) -> MergeResult: ...


def _sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENT_SPLIT.split(text.strip()) if s.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def detect_contradiction(older: str, newer: str) -> bool:
    """Heuristic: same topic but asserting opposite/incompatible facts."""
    wa, wb = _words(older), _words(newer)
    if not wa or not wb:
        return False
    # Negation flip: one negates, the other doesn't, on shared vocabulary.
    neg = {"not", "no", "never", "without", "n't"}
    a_neg = bool(wa & neg)
    b_neg = bool(wb & neg)
    shared = wa & wb
    if a_neg != b_neg and len(shared) >= 2:
        return True
    # Antonym pair present across the two.
    for pair in _ANTONYMS:
        if (pair & wa) and (pair & wb) and (pair & wa) != (pair & wb):
            return True
    # Numeric mismatch on an otherwise-similar statement (e.g. "8 hours" vs "6").
    na, nb = _numbers(older), _numbers(newer)
    if na and nb and na != nb and len(shared) >= 3:
        return True
    return False


class HeuristicMerger:
    """Offline merge: union of distinct sentences; supersede on contradiction."""

    name = "heuristic"

    def decide(self, older: str, newer: str, similarity: float) -> MergeResult:
        if detect_contradiction(older, newer):
            return MergeResult(
                action="supersede",
                content=newer.strip(),
                reason="newer statement contradicts the older one",
            )
        # Merge: keep the newer phrasing, append any older sentences it lacks.
        new_sents = _sentences(newer)
        seen = {s.lower() for s in new_sents}
        merged = list(new_sents)
        for s in _sentences(older):
            key = s.lower()
            # Skip if this sentence is largely subsumed by what we already have.
            if key in seen:
                continue
            if _is_subsumed(s, merged):
                continue
            merged.append(s)
            seen.add(key)
        content = " ".join(merged).strip()
        # If the union is just the newer text, it was a pure restatement.
        reason = (
            "near-duplicate restatement; kept newest phrasing"
            if content == newer.strip()
            else "fused complementary facts into one canonical memory"
        )
        return MergeResult(action="merge", content=content, reason=reason)


def _is_subsumed(sentence: str, existing: list[str], threshold: float = 0.8) -> bool:
    sw = _words(sentence)
    if not sw:
        return True
    for e in existing:
        ew = _words(e)
        if not ew:
            continue
        overlap = len(sw & ew) / len(sw)
        if overlap >= threshold:
            return True
    return False


def _split_decision(text: str) -> tuple[str, str]:
    """Split an LLM merge/supersede reply into (verb, body), robustly.

    Models are inconsistent about the separator after the verb: a real newline,
    a literal ``\\n`` echoed verbatim, a colon, or just a space. Splitting only
    on a real newline silently dropped the fused body when the model echoed the
    literal form, so we normalise the leading verb out and return the remainder.
    """
    text = text.strip()
    upper = text.upper()
    for verb in ("SUPERSEDE", "MERGE"):
        if upper.startswith(verb):
            rest = text[len(verb):]
            # Strip a single leading separator: literal "\n", real newline,
            # colon, or surrounding whitespace.
            rest = rest.lstrip()
            if rest.startswith("\\n"):
                rest = rest[2:]
            elif rest[:1] in ("\n", ":"):
                rest = rest[1:]
            return verb, rest.strip()
    # No recognised verb prefix: fall back to first-line / remainder split.
    head, _, body = text.partition("\n")
    return head.strip(), body.strip()


class HostedMerger:  # pragma: no cover - needs network
    """LLM-powered fusion + contradiction judgement (Claude / GPT)."""

    name = "hosted"

    def __init__(self, model: str):
        self.model = model

    def decide(self, older: str, newer: str, similarity: float) -> MergeResult:
        prompt = (
            "Two memory entries about the same subject may be duplicates, "
            "complementary, or contradictory.\n"
            f"OLDER: {older}\nNEWER: {newer}\n\n"
            "If they contradict, start your reply with SUPERSEDE then the newer "
            "fact on the next line.\n"
            "Otherwise start your reply with MERGE then one concise canonical "
            "memory fusing both on the next line."
        )
        text = self._complete(prompt).strip()
        head, body = _split_decision(text)
        if head.upper().startswith("SUPERSEDE"):
            return MergeResult("supersede", (body or newer).strip(), "LLM: contradiction")
        return MergeResult("merge", (body or newer).strip(), "LLM: fused")

    def _complete(self, prompt: str) -> str:
        if self.model.startswith("claude"):
            import anthropic

            client = anthropic.Anthropic()
            msg = client.messages.create(
                model=self.model,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(b.text for b in msg.content if b.type == "text")
        from openai import OpenAI

        client = OpenAI()
        resp = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
        )
        return resp.choices[0].message.content or ""


def make_merger(model: str) -> Merger:
    if not model or model in ("heuristic", "offline", "local"):
        return HeuristicMerger()
    return HostedMerger(model)
