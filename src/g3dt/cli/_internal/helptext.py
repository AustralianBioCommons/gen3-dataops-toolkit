"""Shared help strings for options that appear across command groups.

--env is offered by ~20 commands; before this module each declared its own
phrasing (five variants had accumulated), so the same option read
differently depending on which command's --help you happened to open. One
constant keeps the story identical everywhere; append command-specific
notes with ``+`` where a command genuinely differs.
"""

ENV_OPT = (
    "Environment; defaults to the current context "
    "(switch with 'g3dt config use', one-shot with --ctx)."
)

ENV_OPT_SYNTH = ENV_OPT + " Production targets require typed confirmation."
