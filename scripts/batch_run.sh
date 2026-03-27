#!/bin/bash
# Run all scenarios in the scenarios/ directory
# Usage: ./scripts/batch_run.sh

set -e
cd "$(dirname "$0")/.."

PASS=0
FAIL=0

for scenario in scenarios/*.yaml; do
    name=$(basename "$scenario" .yaml)
    echo ""
    echo "========================================="
    echo "  Running: $name"
    echo "========================================="

    if uv run python run.py "$scenario" --run-id "batch_${name}_$(date +%s)"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
    fi
done

echo ""
echo "========================================="
echo "  Batch Complete: $PASS passed, $FAIL failed"
echo "========================================="

[ $FAIL -eq 0 ] || exit 1
