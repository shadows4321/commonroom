PY ?= python3
VENV := .venv
BIN := $(abspath $(VENV))/bin
WORLD ?= worlds/house

.PHONY: install test run clean

$(BIN)/python:
	$(PY) -m venv $(VENV)

install: $(BIN)/python
	$(BIN)/pip install -e packages/protocol -e "packages/server[dev]" -e "packages/sdk-python[dev]"

test:
	cd packages/protocol && $(BIN)/python -m pytest -q
	cd packages/server && $(BIN)/python -m pytest -q
	cd packages/sdk-python && $(BIN)/python -m pytest -q

run:
	$(BIN)/commonroom-server run --world $(WORLD) --db world.db

clean:
	rm -rf $(VENV) world.db* .pytest_cache packages/*/.pytest_cache
