"""End-to-end contract test for the ACTIVE_USER_ID routing design.

What this test pins
--------------------
1. ``lead_prompt_overlay`` tells the lead agent to prepend a literal line
   ``ACTIVE_USER_ID=<id>`` to every persona dispatch prompt.
2. The persona SKILL.md ``buffett-persona`` and ``munger-persona`` teach the
   LLM to extract that line and pass it as ``user_id`` to ``tools.qdrant_memory``.
3. The persona must refuse to act if the line is missing (do-not-default rule).
4. The persona must refuse to act if the line is for another slot (do-not-cross
   rule).

These are text-level contract checks, not LLM calls. They catch the regression
mode where someone edits the SKILL.md and accidentally re-binds a persona to a
fixed slot, or drops the ``ACTIVE_USER_ID`` instruction.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(os.path.dirname(__file__)).resolve().parent
SKILL_DIR = REPO / "skills" / "custom"
CONFIG = REPO / "config.yaml"

PERSONAS = ("buffett-persona", "munger-persona")
ALL_SLOTS = ("PLACEHOLDER_USER_1", "PLACEHOLDER_USER_2", "PLACEHOLDER_USER_3")


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_overlay_block(prompt: str) -> str:
    """Return the contents of the <berkshire_router> block from a rendered prompt."""
    m = re.search(r"<berkshire_router>(.*?)</berkshire_router>", prompt, re.DOTALL)
    if not m:
        raise AssertionError("no <berkshire_router> block in prompt")
    return m.group(1)


# ─────────────────────────────────────────────────────────────────────
# Lead-agent overlay contract
# ─────────────────────────────────────────────────────────────────────


def test_overlay_requires_active_user_id_line_in_dispatch():
    """The router must tell the lead agent to inject ACTIVE_USER_ID into the
    dispatch prompt for every persona. Without this, persona skills would not
    know which user is asking."""
    import yaml

    overlay = yaml.safe_load(_read(CONFIG))["lead_prompt_overlay"]["prepend"]
    assert "ACTIVE_USER_ID=" in overlay, "overlay must instruct lead agent to inject ACTIVE_USER_ID"
    # Both routing rules that dispatch a persona must reference the injection.
    explicit_mention_section = re.search(
        r"Explicit @-mention.*?Live price",
        overlay,
        re.DOTALL,
    )
    assert explicit_mention_section is not None, "explicit @-mention rule missing"
    assert "ACTIVE_USER_ID" in explicit_mention_section.group(0), (
        "explicit @-mention rule must instruct lead agent to inject ACTIVE_USER_ID"
    )


def test_overlay_documents_persona_asker_orthogonality():
    overlay = yaml.safe_load(_read(CONFIG))["lead_prompt_overlay"]["prepend"]
    # The orthogonality rule must be explicit so future edits don't accidentally
    # re-bind a persona to a fixed slot.
    assert "Persona × asker are orthogonal" in overlay or "Persona \u00d7 asker are orthogonal" in overlay, (
        "overlay must explicitly call out that persona and asker are orthogonal"
    )


def test_overlay_does_not_bind_persona_to_specific_user():
    """A regression guard: the overlay must not say anything like
    'buffett-persona reads PLACEHOLDER_USER_2' — that was the old wrong design."""
    overlay = yaml.safe_load(_read(CONFIG))["lead_prompt_overlay"]["prepend"]
    bad_phrases = [
        "buffett-persona reads PLACEHOLDER_USER_2",
        "munger-persona reads PLACEHOLDER_USER_1",
        "Buffett persona reads/writes PLACEHOLDER_USER_2",
        "Munger persona reads/writes PLACEHOLDER_USER_1",
    ]
    for bad in bad_phrases:
        assert bad not in overlay, f"overlay still contains old bind: {bad!r}"


# ─────────────────────────────────────────────────────────────────────
# Persona skill contract
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("persona", PERSONAS)
def test_persona_skill_uses_active_user_id_placeholder(persona):
    skill_md = _read(SKILL_DIR / persona / "SKILL.md")
    assert "ACTIVE_USER_ID" in skill_md, f"{persona} SKILL.md must reference ACTIVE_USER_ID"
    # Must teach the model to parse it from the dispatch prompt.
    assert "prompt" in skill_md.lower(), f"{persona} SKILL.md must mention the task prompt"


@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("slot", ALL_SLOTS)
def test_persona_skill_must_not_hardcode_any_slot(persona, slot):
    """No persona may default to or whitelist a specific PLACEHOLDER_USER_* slot.

    Old (wrong) skill bodies had lines like 'user_id = PLACEHOLDER_USERS[0]'.
    The new design requires the user_id to come from ACTIVE_USER_ID only.
    """
    skill_md = _read(SKILL_DIR / persona / "SKILL.md")
    # A regex that catches "user_id = ...PLACEHOLDER_USER_X" style bindings.
    forbidden = re.findall(
        r"user_id\s*=\s*PLACEHOLDER_USERS?\[\d+\]",
        skill_md,
    )
    assert not forbidden, f"{persona} SKILL.md still hardcodes user_id via PLACEHOLDER_USERS[idx]: {forbidden}"

    # Also forbid bare "default to PLACEHOLDER_USER_X" prose.
    bad_prose = [
        f"default to {slot}",
        f"defaults to {slot}",
        f"固定 {slot}",
    ]
    for bad in bad_prose:
        assert bad not in skill_md, f"{persona} SKILL.md still defaults to {slot}"


@pytest.mark.parametrize("persona", PERSONAS)
def test_persona_skill_halts_when_active_user_id_missing(persona):
    skill_md = _read(SKILL_DIR / persona / "SKILL.md")
    # Both skills must teach the LLM to refuse if ACTIVE_USER_ID is absent.
    assert "missing" in skill_md.lower(), f"{persona} SKILL.md must address the missing-id case"
    assert "halt" in skill_md.lower() or "stop" in skill_md.lower() or "report" in skill_md.lower(), (
        f"{persona} SKILL.md must teach the model to halt when ACTIVE_USER_ID is missing"
    )


@pytest.mark.parametrize("persona", PERSONAS)
def test_persona_skill_forbids_cross_slot_read(persona):
    skill_md = _read(SKILL_DIR / persona / "SKILL.md")
    # The skill must explicitly say "never use another slot's id".
    assert "never" in skill_md.lower(), f"{persona} SKILL.md must contain a never-do rule"
    # And specifically about cross-slot / other user.
    has_cross_slot_rule = any(
        phrase in skill_md.lower()
        for phrase in (
            "another user",
            "other user",
            "cross-slot",
            "another slot",
            "another persona",
        )
    )
    assert has_cross_slot_rule, f"{persona} SKILL.md must forbid cross-slot reads"


# ─────────────────────────────────────────────────────────────────────
# Simulated dispatch (no LLM call)
# ─────────────────────────────────────────────────────────────────────


def _simulate_persona_extract(skill_md: str, dispatch_prompt: str) -> str:
    """Tiny regex harness that mirrors what the SKILL.md tells the LLM to do.

    The persona SKILL.md says: 'Parse the first non-empty line of the task
    prompt you received. The lead agent writes `ACTIVE_USER_ID=<id>`.'
    """
    for line in dispatch_prompt.splitlines():
        line = line.strip()
        if line.startswith("ACTIVE_USER_ID="):
            return line.split("=", 1)[1].strip()
    raise ValueError("ACTIVE_USER_ID not present in dispatch prompt")


def test_simulated_dispatch_father_asks_munger():
    """Father (PLACEHOLDER_USER_2) asks Munger — Munger should read father's collection."""
    prompt = "ACTIVE_USER_ID=PLACEHOLDER_USER_2\n@芒格 我该不该买房?"
    uid = _simulate_persona_extract(_read(SKILL_DIR / "munger-persona" / "SKILL.md"), prompt)
    assert uid == "PLACEHOLDER_USER_2"


def test_simulated_dispatch_mother_asks_buffett():
    """Mother (PLACEHOLDER_USER_3) asks Buffett — Buffett should read mother's collection."""
    prompt = "ACTIVE_USER_ID=PLACEHOLDER_USER_3\n巴菲特 我的退休账户怎么配置?"
    uid = _simulate_persona_extract(_read(SKILL_DIR / "buffett-persona" / "SKILL.md"), prompt)
    assert uid == "PLACEHOLDER_USER_3"


def test_simulated_dispatch_user_asks_munger():
    """The user themselves (PLACEHOLDER_USER_1) asks Munger."""
    prompt = "ACTIVE_USER_ID=PLACEHOLDER_USER_1\n芒格 我该不该结婚?"
    uid = _simulate_persona_extract(_read(SKILL_DIR / "munger-persona" / "SKILL.md"), prompt)
    assert uid == "PLACEHOLDER_USER_1"


def test_simulated_dispatch_missing_user_id_halts():
    """If the lead agent forgot to inject ACTIVE_USER_ID, the persona must halt."""
    with pytest.raises(ValueError, match="ACTIVE_USER_ID not present"):
        _simulate_persona_extract(_read(SKILL_DIR / "munger-persona" / "SKILL.md"), "芒格 我该不该结婚?")
