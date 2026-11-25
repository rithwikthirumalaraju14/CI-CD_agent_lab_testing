"""References and evidence collected from one workflow run attempt."""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_RUN_PATH = re.compile(
    r"/(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/"
    r"(?P<repository>[A-Za-z0-9_.-]{1,100})/actions/runs/"
    r"(?P<run_id>[1-9][0-9]{0,19})(?:/attempts/(?P<attempt>[1-9][0-9]{0,5}))?/?"
)
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure", "action_required"})


@dataclass(frozen=True, slots=True)
class RunReference:
    owner: str
    repository: str
    run_id: int
    attempt: int | None = None

    @classmethod
    def parse(cls, url: str) -> "RunReference":
        url = url.strip()
        parsed = urlsplit(url)
        match = _RUN_PATH.fullmatch(parsed.path)
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != "github.com"
            or any(character.isspace() for character in url)
            or not match
            or match["repository"] in {".", ".."}
        ):
            raise ValueError(
                "Paste a GitHub Actions run URL: https://github.com/OWNER/REPO/actions/runs/RUN_ID"
            )
        return cls(
            owner=match["owner"],
            repository=match["repository"],
            run_id=int(match["run_id"]),
            attempt=int(match["attempt"]) if match["attempt"] else None,
        )

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"

    @property
    def api_base(self) -> str:
        return f"/repos/{self.full_name}/actions/runs/{self.run_id}"

    @property
    def html_url(self) -> str:
        url = f"https://github.com/{self.full_name}/actions/runs/{self.run_id}"
        return f"{url}/attempts/{self.attempt}" if self.attempt else url


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class ApiDocument:
    endpoint: str
    content: bytes


@dataclass(slots=True)
class CollectedRun:
    reference: RunReference
    run: dict
    jobs: list[dict]
    source_ref: str
    sources: list[SourceFile] = field(default_factory=list)
    api_documents: list[ApiDocument] = field(default_factory=list)
    log_archive: bytes | None = None
    artifacts: list[dict] = field(default_factory=list)
    attachments: list[SourceFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
