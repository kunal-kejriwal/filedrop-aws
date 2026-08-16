# Filedrop — dev workflow
# All targets assume you run them from filedrop-aws/.

SHELL := /bin/bash
UV    ?= uv
PY    ?= python3.12

.PHONY: install lint typecheck test synth deploy destroy load-test redrive-dry help

help:
	@echo "Targets: install lint typecheck test synth deploy destroy load-test redrive-dry"

install:
	@if command -v $(UV) >/dev/null 2>&1; then \
		$(UV) sync --extra dev; \
	else \
		$(PY) -m venv .venv && . .venv/bin/activate && \
		pip install -e ".[dev]"; \
	fi

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy shared functions infra

test:
	pytest tests/unit -v

synth:
	cdk synth

# SENDER_EMAIL is required — will be verified in SES.
deploy:
	@if [ -z "$$SENDER_EMAIL" ]; then echo "SENDER_EMAIL not set"; exit 1; fi
	cdk deploy --all --require-approval never \
		--context senderEmail=$$SENDER_EMAIL \
		$(if $(ALARM_EMAIL),--context alarmEmail=$(ALARM_EMAIL),)

destroy:
	cdk destroy --all --force

load-test:
	@if [ -z "$$FILEDROP_API_URL" ]; then echo "FILEDROP_API_URL not set"; exit 1; fi
	@if [ -z "$$TEST_EMAIL" ]; then echo "TEST_EMAIL not set (must be SES-verified in sandbox)"; exit 1; fi
	python scripts/load_test.py --count 50 --api-url $$FILEDROP_API_URL --email $$TEST_EMAIL

redrive-dry:
	python scripts/dlq_redrive.py --queue filedrop-notify-dlq --limit 10 --dry-run
