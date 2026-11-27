import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_repacked_release_bundle():
    request = json.loads("{\"entries\": [{\"name\": \"plugin.txt\", \"content\": \"release\\n\", \"mode\": 493}, {\"name\": \"assets/banner\", \"content\": \"ok\", \"mode\": 384}], \"runner_clock\": [2025, 7, 2, 9, 10, 0]}")
    expected = json.loads("{\"zip_b64\": \"UEsDBBQAAAAAAAAAIVhH3dx5AgAAAAIAAAANAAAAYXNzZXRzL2Jhbm5lcm9rUEsDBBQAAAAAAAAAIVhJLU9RCAAAAAgAAAAKAAAAcGx1Z2luLnR4dHJlbGVhc2UKUEsBAhQDFAAAAAAAAAAhWEfd3HkCAAAAAgAAAA0AAAAAAAAAAAAAAKQBAAAAAGFzc2V0cy9iYW5uZXJQSwECFAMUAAAAAAAAACFYSS1PUQgAAAAIAAAACgAAAAAAAAAAAAAApAEtAAAAcGx1Z2luLnR4dFBLBQYAAAAAAgACAHMAAABdAAAAAAA=\"}")
    assert run(request, ROOT) == expected
