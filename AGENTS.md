# Project requirements

Before changing backtest behavior, read `docs/requirements_baseline.md` and the relevant existing modules. The user's latest instructions take precedence over that baseline; record subsequent confirmed changes there so experimental parameters do not become permanent requirements.

Preserve the six-column Portfolio APIs, opening-date attribution, pending-settlement handling, and standalone premium/discount interface when extending the lifecycle model. Keep pricing, payment deadlines, and actual cash receipt distinct. Test the complete call path rather than relying on an unused implementation.

The Desktop checkout contains the user's uncommitted work. Do not overwrite it or assume it is identical to this managed worktree. Publication or merging must follow the scope of the user's applicable request.
