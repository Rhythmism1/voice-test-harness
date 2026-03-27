#!/bin/bash
# Run a single test scenario
# Usage: ./scripts/run_scenario.sh scenarios/basic_english.yaml

set -e
cd "$(dirname "$0")/.."

if [ -z "$1" ]; then
    echo "Usage: $0 <scenario.yaml> [--run-id <id>]"
    exit 1
fi

uv run python run.py "$@"
