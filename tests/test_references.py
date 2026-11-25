import pytest

from buildtrace.models import RunReference


@pytest.mark.parametrize(
    ("url", "attempt"),
    [
        ("https://github.com/example/invoice/actions/runs/12345", None),
        (" https://github.com/example/invoice/actions/runs/12345/ ", None),
        ("https://github.com/example/invoice/actions/runs/12345/attempts/2", 2),
        ("https://github.com/example/invoice/actions/runs/12345?check_suite_focus=true#step:8:1", None),
    ],
)
def test_actions_run_urls(url, attempt):
    reference = RunReference.parse(url)
    assert reference.full_name == "example/invoice"
    assert reference.run_id == 12345
    assert reference.attempt == attempt


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://github.com/example/invoice/actions/runs/12345",
        "https://localhost/example/invoice/actions/runs/12345",
        "https://github.com.evil.test/example/invoice/actions/runs/12345",
        "https://token@github.com/example/invoice/actions/runs/12345",
        "https://github.com:443/example/invoice/actions/runs/12345",
        "https://github.com/example/invoice/actions/runs/0",
        "https://github.com/example/invoice/actions/runs/not-a-number",
        "https://github.com/example/invoice/actions/runs/12345/attempts/0",
        "https://github.com/example/invoice/actions/runs/12345/jobs/1",
        "https://github.com/example/../actions/runs/12345",
        "https://github.com/example/invoice%2fother/actions/runs/12345",
    ],
)
def test_other_urls_are_rejected(url):
    with pytest.raises(ValueError, match="GitHub Actions run URL"):
        RunReference.parse(url)
