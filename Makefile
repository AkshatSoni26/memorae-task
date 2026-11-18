PORT ?= 8000

.PHONY: help install server ngrok dev cli

help:
	@echo "make install  - install dependencies (uv sync)"
	@echo "make server   - start FastAPI server on port $(PORT) with auto-reload"
	@echo "make ngrok    - start ngrok tunnel to port $(PORT) (run in a 2nd terminal)"
	@echo "make dev      - open ngrok in a new window, then start the server here"
	@echo "make cli URL=https://meet.google.com/xxx-yyyy-zzz - dispatch bot from terminal"

install:
	uv sync

server:
	uv run uvicorn main:app --reload --port $(PORT)

ngrok:
	ngrok http $(PORT)

dev:
	powershell -NoProfile -Command "Start-Process ngrok -ArgumentList 'http','$(PORT)'"
	uv run uvicorn main:app --reload --port $(PORT)

cli:
	uv run python cli.py $(URL)
