.PHONY: install dev test demo lint api docker-up clean

install:
	pip install -e ".[dev]"

dev: install
	cp -n .env.example .env || true

test:
	pytest tests/unit -q --tb=short

demo:
	python -m src.cli demo

lint:
	ruff check src tests
	mypy src || true

api:
	uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

clean:
	rm -rf .venv dist build *.egg-info .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
