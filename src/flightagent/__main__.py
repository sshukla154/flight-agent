"""``python -m flightagent`` entry point.

The Phase 1 stub here deferred to "later phases" for the real Typer CLI
(T16, ``flightagent.cli``). It now just delegates to it, so ``python -m
flightagent ...`` and the installed ``flightagent`` console script
(``[project.scripts]`` in pyproject.toml) run the identical command.
"""

from flightagent.cli import main

if __name__ == "__main__":
    main()
