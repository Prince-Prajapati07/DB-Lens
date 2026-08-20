"""GitHub draft Pull Request payload and publishing support for PlanPatch."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from planpatch.migrations import MigrationArtifact
from planpatch.planner import PlanEvidence


GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
HTTP_TIMEOUT_SECONDS = 10
_REPOSITORY_PATTERN = re.compile(
    r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]+\Z"
)


@dataclass(frozen=True, slots=True)
class PRPayload:
    """Deterministic content required to open a draft Pull Request."""

    branch_name: str
    title: str
    markdown_body: str


class GitHubAPIError(RuntimeError):
    """A sanitized GitHub API or response error."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        suffix = "" if status is None else f" (HTTP {status})"
        super().__init__(f"{code}{suffix}")


def build_pr_payload(
    table_name: str,
    column_name: str,
    evidence: PlanEvidence,
    artifact: MigrationArtifact,
) -> PRPayload:
    """Build a deterministic, review-focused draft PR payload."""
    if not table_name.strip() or not column_name.strip():
        raise ValueError("table_name and column_name are required")

    branch_name = f"planpatch/{artifact.index_name}"
    title = f"perf(db): add index for {table_name}.{column_name}"
    markdown_body = (
        "## DB-Lens PlanPatch recommendation\n\n"
        "This draft contains planner-estimated evidence for human review. "
        "It does not represent measured production latency improvement.\n\n"
        f"- **Target table:** `{_escape_code_span(table_name)}`\n"
        f"- **Candidate column:** `{_escape_code_span(column_name)}`\n"
        f"- **Virtual index used:** {'Yes' if evidence.used_virtual_index else 'No'}\n\n"
        "| Planner estimate | Cost |\n"
        "|---|---:|\n"
        f"| Baseline Cost | {evidence.baseline_cost:.2f} |\n"
        f"| Optimized Cost | {evidence.optimized_cost:.2f} |\n"
        f"| % Reduction | {evidence.cost_reduction_pct:.2f}% |\n\n"
        "## Proposed migration\n\n"
        "```sql\n"
        f"{artifact.up_sql_content.rstrip()}\n"
        "```\n\n"
        "> **Human review required:** `CREATE INDEX CONCURRENTLY` must run "
        "outside a transaction block. Review write, storage, and operational "
        "impact before deployment.\n"
    )
    return PRPayload(
        branch_name=branch_name,
        title=title,
        markdown_body=markdown_body,
    )


def publish_draft_pr(payload: PRPayload, repo: str, token: str) -> str:
    """Return an existing PR URL or create and return a new draft PR URL.

    The migration branch must already exist on GitHub. This function only
    performs PR discovery and creation; it does not create branches or commits.
    """
    _validate_publish_inputs(payload, repo, token)
    owner = repo.split("/", maxsplit=1)[0]

    query = urlencode(
        {
            "state": "all",
            "head": f"{owner}:{payload.branch_name}",
            "per_page": "1",
        }
    )
    existing = _request_json(
        method="GET",
        url=f"{GITHUB_API_URL}/repos/{repo}/pulls?{query}",
        token=token,
        expected_status=200,
    )
    if not isinstance(existing, list):
        raise GitHubAPIError("invalid_pull_request_search_response")
    if existing:
        return _extract_pr_url(existing[0])

    repository = _request_json(
        method="GET",
        url=f"{GITHUB_API_URL}/repos/{repo}",
        token=token,
        expected_status=200,
    )
    if not isinstance(repository, dict):
        raise GitHubAPIError("invalid_repository_response")
    default_branch = repository.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise GitHubAPIError("missing_default_branch")

    created = _request_json(
        method="POST",
        url=f"{GITHUB_API_URL}/repos/{repo}/pulls",
        token=token,
        expected_status=201,
        payload={
            "title": payload.title,
            "head": payload.branch_name,
            "base": default_branch,
            "body": payload.markdown_body,
            "draft": True,
        },
    )
    return _extract_pr_url(created)


def _validate_publish_inputs(payload: PRPayload, repo: str, token: str) -> None:
    if not _REPOSITORY_PATTERN.fullmatch(repo):
        raise ValueError("repo must use the owner/repository format")
    if not token.strip():
        raise ValueError("GitHub token is required")
    if not payload.branch_name.strip() or not payload.title.strip():
        raise ValueError("PR branch and title are required")


def _request_json(
    *,
    method: str,
    url: str,
    token: str,
    expected_status: int,
    payload: dict[str, object] | None = None,
) -> object:
    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "db-lens-planpatch",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url=url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = response.status
            response_body = response.read()
    except HTTPError as exc:
        # Deliberately avoid including response bodies or request headers: API
        # error content can contain repository details, and headers hold tokens.
        exc.close()
        raise GitHubAPIError("github_http_error", status=exc.code) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise GitHubAPIError("github_network_error") from exc

    if status != expected_status:
        raise GitHubAPIError("unexpected_github_status", status=status)
    try:
        return json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubAPIError("invalid_github_json") from exc


def _extract_pr_url(value: object) -> str:
    if not isinstance(value, dict):
        raise GitHubAPIError("invalid_pull_request_response")
    html_url = value.get("html_url")
    if not isinstance(html_url, str) or not html_url.startswith("https://github.com/"):
        raise GitHubAPIError("missing_pull_request_url")
    return html_url


def _escape_code_span(value: str) -> str:
    return value.replace("`", "\\`")
