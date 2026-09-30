"""Contract tests for the 'soul' layer of buffett-persona and munger-persona.

These are text-level guarantees that the SKILL.md files carry enough
character to behave as personas, not as generic value-investing /
rationalist LLM defaults. They guard against accidental soul erosion:
some future edit deletes the rich soul section and the personas
collapse into "advice LLM with bulleted principles".

What this test pins
--------------------
For each persona SKILL.md:
1. Contains a clearly labelled "Soul" section with the explanatory
   framing line "what makes you <name>, not a generic ...".
2. Carries the four core-belief paragraphs at depth (each one
   approximately a long paragraph, not a one-liner).
3. Carries the "Stance on common ... themes" section with at least
   6 markers per persona (speculation, leverage, real-estate,
   concentrated stock, market timing, inheritance, career, children,
   etc.).
4. Carries the "Anti-patterns" section with explicit never-say items.
5. States the priority rule: the soul is a *thinking frame*, not a
   checklist; the 150-350 word budget still wins.
6. Persona-orthogonality: nothing re-binds a persona to a fixed slot.

These tests are descriptive, not normative — they do not judge whether
Buffett's or Munger's beliefs are right. They only ensure that any
future change that erodes the soul to bullet points will fail loudly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO = Path(os.path.dirname(__file__)).resolve().parent
SKILL_DIR = REPO / "skills" / "custom"
PERSONAS = ("buffett-persona", "munger-persona")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Section presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("persona", PERSONAS)
def test_soul_section_present(persona: str) -> None:
    """Each SKILL.md must contain a Soul section with the distinctive
    'what makes you <name>, not a generic ...' framing line."""
    text = _read(SKILL_DIR / persona / "SKILL.md")
    assert "## Soul" in text, f"{persona} SKILL.md must include a '## Soul' section"
    # Framing line that distinguishes from generic assistant defaults.
    assert "not a" in text.lower() and "generic" in text.lower(), (
        f"{persona} SKILL.md Soul must explicitly contrast itself with a generic LLM"
    )


@pytest.mark.parametrize("persona", PERSONAS)
def test_core_beliefs_present_and_substantial(persona: str) -> None:
    """Each SKILL.md must enumerate the six core beliefs as numbered paragraphs.

    A regression here means someone replaced the deep soul with bullet
    points. Each numbered item must be a *paragraph* (multiple lines of
    prose), not a single-line bullet.
    """
    text = _read(SKILL_DIR / persona / "SKILL.md")
    # Pull the "Core beliefs" block by anchoring on the header line and
    # terminating at the next line that starts with "**" or "##".
    m = re.search(r"\*\*Core beliefs[\s\S]*?(?=\n\*\*[A-Z]|\n## )", text)
    assert m is not None, f"{persona} SKILL.md must contain a 'Core beliefs' block"
    block = m.group(0)
    # Must enumerate at least 5 numbered items.
    items = re.findall(r"^\d+\.\s+\*\*", block, flags=re.MULTILINE)
    assert len(items) >= 5, (
        f"{persona} SKILL.md Core beliefs must have at least 5 numbered items, got {len(items)}"
    )
    # Each numbered item must contain a non-trivial sentence (>80 chars),
    # not just a one-line slogan.
    for i, item in enumerate(items, start=1):
        # Slice from item start to next "^\d+\.\s+\*\*" or end-of-block.
        m_item = re.search(
            rf"^{i}\.\s+\*\*[\s\S]*?(?=^\d+\.\s+\*\*|\Z)", block, flags=re.MULTILINE
        )
        assert m_item is not None
        body = m_item.group(0)
        # Strip the leading "N. **title**. " header and count remaining prose.
        cleaned = re.sub(r"^\d+\.\s+\*\*[^*]+\*\*\s*", "", body, flags=re.MULTILINE).strip()
        assert len(cleaned) >= 80, (
            f"{persona} SKILL.md core belief #{i} is suspiciously short ({len(cleaned)} chars); "
            "soul must be a paragraph, not a slogan"
        )


@pytest.mark.parametrize("persona", PERSONAS)
def test_thinking_framework_present(persona: str) -> None:
    """Each SKILL.md must include a 'Thinking framework' section that
    names the cognitive moves the persona uses."""
    text = _read(SKILL_DIR / persona / "SKILL.md")
    assert "**Thinking framework**" in text or "**Thinking framework " in text or "**Thinking framework\u2014" in text, (
        f"{persona} SKILL.md must include a 'Thinking framework' block"
    )


@pytest.mark.parametrize("persona", PERSONAS)
def test_stance_on_common_themes_present(persona: str) -> None:
    """Each SKILL.md must include a 'Stance on common' section with
    at least 6 topic markers, covering life and investment topics."""
    text = _read(SKILL_DIR / persona / "SKILL.md")
    m = re.search(r"\*\*Stance on common[\s\S]*?(?=\n\*\*[A-Z]|\n## )", text)
    assert m is not None, f"{persona} SKILL.md must contain a 'Stance on common' block"
    block = m.group(0)
    # Stance bullets are formatted as "- *Theme:* prose" — but the theme
    # title may contain internal ":" or ".". Count any non-empty bullet
    # line in the block.
    bullets = re.findall(r"^\s*-\s+\S+", block, flags=re.MULTILINE)
    assert len(bullets) >= 6, (
        f"{persona} SKILL.md must list at least 6 stance themes, got {len(bullets)}: {bullets!r}"
    )
    # Coverage: at least one investment/life topic and one life/carele topic.
    blob = "\n".join(bullets).lower()
    has_invest = any(k in blob for k in ("invest", "lever", "real estate", "speculation", "market"))
    has_life = any(k in blob for k in ("marriage", "career", "children", "family", "health", "friend", "social"))
    assert has_invest, f"{persona} SKILL.md stance must cover investment themes"
    assert has_life, f"{persona} SKILL.md stance must cover life / family themes"


@pytest.mark.parametrize("persona", PERSONAS)
def test_anti_patterns_present(persona: str) -> None:
    """Each SKILL.md must include an 'Anti-patterns' section with
    explicit never-say items."""
    text = _read(SKILL_DIR / persona / "SKILL.md")
    assert "**Anti-patterns**" in text or "**Anti-patterns " in text or "**Anti-patterns\u2014" in text, (
        f"{persona} SKILL.md must include an 'Anti-patterns' block"
    )
    # The section must contain at least 3 never-say entries.
    block = re.search(r"\*\*Anti-patterns[\s\S]*?(?=\n\*\*[A-Z]|\n## )", text)
    assert block is not None
    entries = re.findall(r"^\s*-\s+", block.group(0), flags=re.MULTILINE)
    assert len(entries) >= 3, (
        f"{persona} SKILL.md Anti-patterns must have at least 3 entries, got {len(entries)}"
    )


# ---------------------------------------------------------------------------
# Soul-vs-budget priority rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("persona", PERSONAS)
def test_soul_priority_rule_present(persona: str) -> None:
    """Soul must declare it is a thinking frame and that the word
    budget still wins; otherwise the soul will be recited instead of
    used."""
    text = _read(SKILL_DIR / persona / "SKILL.md").lower()
    assert "thinking frame" in text, (
        f"{persona} SKILL.md must declare the soul is a 'thinking frame'"
    )
    assert "word budget" in text or "350 word" in text or "150" in text, (
        f"{persona} SKILL.md must reference the 150-350 word budget so the soul does not inflate answers"
    )


# ---------------------------------------------------------------------------
# Per-persona soul fingerprints
# ---------------------------------------------------------------------------


def test_buffett_soul_has_specific_beliefs() -> None:
    text = _read(SKILL_DIR / "buffett-persona" / "SKILL.md").lower()
    # Buffett-specific anchors.
    anchors = [
        "never lose money",
        "margin of safety",
        "intrinsic value",
        "circle of competence",
        "opportunity cost",
        "berkshire",
    ]
    for a in anchors:
        assert a in text, f"buffett-persona SKILL.md must mention Buffett-specific concept: {a!r}"


def test_munger_soul_has_specific_beliefs() -> None:
    text = _read(SKILL_DIR / "munger-persona" / "SKILL.md").lower()
    # Munger-specific anchors.
    anchors = [
        "invert",
        "incentive",
        "latticework",
        "mental model",
        "sit",
        "character",
    ]
    for a in anchors:
        assert a in text, f"munger-persona SKILL.md must mention Munger-specific concept: {a!r}"


# ---------------------------------------------------------------------------
# Persona × asker orthogonality must survive the soul rewrite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("persona", PERSONAS)
def test_soul_does_not_rebind_persona_to_slot(persona: str) -> None:
    """Even with the soul added, no persona may be re-bound to a fixed
    PLACEHOLDER_USER_* slot."""
    text = _read(SKILL_DIR / persona / "SKILL.md")
    bad_phrases = [
        "buffett-persona reads PLACEHOLDER_USER_2",
        "munger-persona reads PLACEHOLDER_USER_1",
    ]
    for bad in bad_phrases:
        assert bad not in text, f"{persona} SKILL.md re-binds persona to a slot: {bad!r}"
    # And no hard-coded "default to PLACEHOLDER_USER_X" anywhere in the soul.
    bad_prose = [
        "default to PLACEHOLDER_USER_1",
        "default to PLACEHOLDER_USER_2",
        "default to PLACEHOLDER_USER_3",
        "固定 PLACEHOLDER_USER_1",
        "固定 PLACEHOLDER_USER_2",
        "固定 PLACEHOLDER_USER_3",
    ]
    for bad in bad_prose:
        assert bad not in text, f"{persona} SKILL.md soul re-binds persona to slot via {bad!r}"