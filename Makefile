.PHONY: setup up dev down logs test demo samples
setup:      ## create venv + install deps
	python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
up:         ## whole stack in Docker
	docker compose up --build -d
dev:        ## Docker with hot reload
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
down:
	docker compose down
logs:
	docker compose logs -f api worker
test:       ## unit + integration tests (no Docker, no cloud accounts needed)
	python -m pytest -q
demo:       ## full pipeline on a sample file, fully in-memory
	python -m scripts.demo_local samples/sample_insurance_letter.pdf --ask "When are the photographs due?"
samples:
	python scripts/make_samples.py
