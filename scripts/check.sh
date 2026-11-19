#!/bin/sh
# Run every check of the repo: lint, format check, app tests, service tests.
set -eu
cd "$(dirname "$0")/.."
ruff format --check .
ruff check .
python3 -m unittest discover -s tests -t .
python3 -m unittest discover -s service/tests -t service
