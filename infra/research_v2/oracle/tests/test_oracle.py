import json
import os
from pathlib import Path
import pytest
from bench_app.app import run

ROOT = Path(os.environ["BENCH_PROJECT_ROOT"]) if "BENCH_PROJECT_ROOT" in os.environ else Path(__file__).resolve().parents[1]

def test_runner_clock_changes():
    request = json.loads("{\"entries\": [{\"name\": \"assets/banner\", \"content\": \"ok\", \"mode\": 384}, {\"name\": \"plugin.txt\", \"content\": \"release\\n\", \"mode\": 493}], \"runner_clock\": [2026, 9, 6, 4, 2, 0]}")
    expected = json.loads("{\"zip_b64\": \"UEsDBBQAAAAAAAAAIVhH3dx5AgAAAAIAAAANAAAAYXNzZXRzL2Jhbm5lcm9rUEsDBBQAAAAAAAAAIVhJLU9RCAAAAAgAAAAKAAAAcGx1Z2luLnR4dHJlbGVhc2UKUEsBAhQDFAAAAAAAAAAhWEfd3HkCAAAAAgAAAA0AAAAAAAAAAAAAAKQBAAAAAGFzc2V0cy9iYW5uZXJQSwECFAMUAAAAAAAAACFYSS1PUQgAAAAIAAAACgAAAAAAAAAAAAAApAEtAAAAcGx1Z2luLnR4dFBLBQYAAAAAAgACAHMAAABdAAAAAAA=\"}")
    assert run(request, ROOT) == expected


def test_different_permissions():
    request = json.loads("{\"entries\": [{\"name\": \"a\", \"content\": \"one\", \"mode\": 256}, {\"name\": \"b\", \"content\": \"\", \"mode\": 511}], \"runner_clock\": [2025, 7, 2, 9, 10, 0]}")
    expected = json.loads("{\"zip_b64\": \"UEsDBBQAAAAAAAAAIVjxhmx6AwAAAAMAAAABAAAAYW9uZVBLAwQUAAAAAAAAACFYAAAAAAAAAAAAAAAAAQAAAGJQSwECFAMUAAAAAAAAACFY8YZsegMAAAADAAAAAQAAAAAAAAAAAAAApAEAAAAAYVBLAQIUAxQAAAAAAAAAIVgAAAAAAAAAAAAAAAABAAAAAAAAAAAAAACkASIAAABiUEsFBgAAAAACAAIAXgAAAEEAAAAAAA==\"}")
    assert run(request, ROOT) == expected


def test_unicode_path_and_payload():
    request = json.loads("{\"entries\": [{\"name\": \"t/\\u00e9t\\u00e9\", \"content\": \"\\u0394\", \"mode\": 493}, {\"name\": \"Z\", \"content\": \"\\u5927\", \"mode\": 493}], \"runner_clock\": [2025, 7, 2, 9, 10, 0]}")
    expected = json.loads("{\"zip_b64\": \"UEsDBBQAAAAAAAAAIVj/hLHSAwAAAAMAAAABAAAAWuWkp1BLAwQUAAAIAAAAACFYYr/C4wIAAAACAAAABwAAAHQvw6l0w6nOlFBLAQIUAxQAAAAAAAAAIVj/hLHSAwAAAAMAAAABAAAAAAAAAAAAAACkAQAAAABaUEsBAhQDFAAACAAAAAAhWGK/wuMCAAAAAgAAAAcAAAAAAAAAAAAAAKQBIgAAAHQvw6l0w6lQSwUGAAAAAAIAAgBkAAAASQAAAAAA\"}")
    assert run(request, ROOT) == expected
