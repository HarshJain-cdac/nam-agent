# nam-agent: the shell commands you would otherwise type.
PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python)
# the evals never call a model: a placeholder key keeps the client from asking for one
EVAL_ENV := NAM_PROVIDER=anthropic ANTHROPIC_API_KEY=offline NAM_SMALL_MODEL=s

.PHONY: serve eval nam-eval lint

serve:          ## the server: the chat page, /api/* and /v1/nam/* on http://127.0.0.1:7777
	$(PY) -m nam_agent serve

eval:           ## every deterministic eval (no model, no network)
	$(EVAL_ENV) $(PY) -m pytest -q -p no:cacheprovider evals/deterministic

nam-eval:       ## the NAM evals and their metric report
	$(EVAL_ENV) $(PY) -m pytest -q -p no:cacheprovider evals/deterministic/nam
	$(EVAL_ENV) $(PY) scripts/nam_eval_report.py

lint:
	$(PY) -m ruff check nam_agent evals scripts
