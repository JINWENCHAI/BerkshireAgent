"""Built-in subagent configurations."""

from .bash_agent import BASH_AGENT_CONFIG
from .buffett_agent import BUFFETT_CONFIG
from .general_purpose import GENERAL_PURPOSE_CONFIG
from .munger_agent import MUNGER_CONFIG

__all__ = [
    "GENERAL_PURPOSE_CONFIG",
    "BASH_AGENT_CONFIG",
    "MUNGER_CONFIG",
    "BUFFETT_CONFIG",
]

# Registry of built-in subagents.
#
# Munger and Buffett are *independent thinkers*, not persona overlays: see
# ``munger_agent.py`` / ``buffett_agent.py`` for the rationale. The lead agent
# decides between them (and against ``general-purpose``) by the specialist /
# context-isolation cost model in ``deerflow.subagents.AGENTS.md``.
BUILTIN_SUBAGENTS = {
    "general-purpose": GENERAL_PURPOSE_CONFIG,
    "bash": BASH_AGENT_CONFIG,
    "munger": MUNGER_CONFIG,
    "buffett": BUFFETT_CONFIG,
}
