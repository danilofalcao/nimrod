"""Nimrod worklog: cross-session work memory for coding agents.

This is deliberately *not* a facts store. It records what each agent session
did -- intents, files touched, commands run, outcomes -- so the next session
(any agent) can pick up where the previous one stopped. Extraction is fully
deterministic and reads the CLIs' own session stores; no model is involved in
capturing work.
"""

__version__ = "0.1.0"
