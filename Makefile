PY ?= python3
VENV := .venv
BIN := $(abspath $(VENV))/bin
WORLD ?= worlds/town
# make run HOST=0.0.0.0 to accept connections from other machines
HOST ?= 127.0.0.1
PORT ?= 8765
DB ?= world.db

.PHONY: install test run blockout clean

$(BIN)/python:
	$(PY) -m venv $(VENV)

install: $(BIN)/python
	$(BIN)/pip install -e packages/protocol -e "packages/server[dev]" -e "packages/sdk-python[dev]"

test:
	cd packages/protocol && $(BIN)/python -m pytest -q
	cd packages/server && $(BIN)/python -m pytest -q
	cd packages/sdk-python && $(BIN)/python -m pytest -q

run:
	$(BIN)/commonroom-server run --world $(WORLD) --db $(DB) --host $(HOST) --port $(PORT)

blockout:  # regenerate the placeholder town and houses, then re-import them
	$(BIN)/python tools/make_blockout.py
	$(BIN)/commonroom-server import-glb worlds/town/town.glb --world worlds/town
	for owner in mia otto; do $(BIN)/commonroom-server import-glb worlds/town/houses/$$owner.glb --world worlds/town --owner $$owner; done

clean:
	rm -rf $(VENV) world.db* .pytest_cache packages/*/.pytest_cache
