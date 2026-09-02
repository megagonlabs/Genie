#!/usr/bin/env bash

# Regenerate `ruff_report.txt` at the repository root.

set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."

ruff check . --output-format grouped --output-file ruff_report.txt
