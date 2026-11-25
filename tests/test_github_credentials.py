from types import SimpleNamespace

from buildtrace.github import resolve_github_token


def test_explicit_environment_token_has_priority(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "explicit-test-token")
    monkeypatch.setattr("buildtrace.github.subprocess.run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Credential lookup was unnecessary")))
    assert resolve_github_token() == "explicit-test-token"


def test_named_git_account_lookup_is_noninteractive_and_does_not_print_token(monkeypatch, capsys):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("BUILDTRACE_GITHUB_ACCOUNT", "example-account")
    monkeypatch.setattr("buildtrace.github.shutil.which", lambda name: "git" if name == "git" else None)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="username=example-account\npassword=credential-test-token\n")

    monkeypatch.setattr("buildtrace.github.subprocess.run", run)
    assert resolve_github_token() == "credential-test-token"
    assert calls[0][1]["env"]["GCM_INTERACTIVE"] == "never"
    assert calls[0][1]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "username=example-account\n" in calls[0][1]["input"]
    assert "credential-test-token" not in capsys.readouterr().out


def test_account_name_cannot_inject_credential_fields(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("BUILDTRACE_GITHUB_ACCOUNT", "example\nhost=other.test")
    monkeypatch.setattr("buildtrace.github.shutil.which", lambda name: "git" if name == "git" else None)
    monkeypatch.setattr("buildtrace.github.subprocess.run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Invalid account was used")))
    assert resolve_github_token() is None
