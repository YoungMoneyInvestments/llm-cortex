from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_benchmark_runner_lists_cases():
    root = Path(__file__).resolve().parents[1]
    script = root / "benchmarks" / "retrieval_cases_runner.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--list"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "coverage_vs_recency" in completed.stdout
