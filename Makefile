.PHONY: venv db migrate test api mcp consolidate extract

venv:
	python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

db:
	docker compose up -d postgres

migrate:
	.venv/bin/memoramum-migrate

test:
	MEMORAMUM_TEST_DATABASE_URL=$${MEMORAMUM_TEST_DATABASE_URL:-postgresql://memoramum:memoramum@127.0.0.1:5432/memoramum_test} \
	.venv/bin/pytest -q

api: migrate
	.venv/bin/memoramum-api

mcp: migrate
	MEMORAMUM_MCP_TRANSPORT=http .venv/bin/memoramum-mcp

consolidate: migrate
	.venv/bin/memoramum-consolidate

extract: migrate
	.venv/bin/memoramum-extract
