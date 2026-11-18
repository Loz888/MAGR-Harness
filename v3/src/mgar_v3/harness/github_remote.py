"""Bounded GitHub ports for the separate, explicitly approved coding channel.

The analysis MCP client is never given these tools or the write credential.
Every mutation is called by the journalled delivery transport, not by a model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote

import httpx

from mgar_v3.domain import DomainError
from mgar_v3.effects.dispatcher import DefinitiveFailure
from mgar_v3.repair.pr_delivery import DraftPullRequest


class GitHubReader(Protocol):
    def branch_sha(self, repository: str, branch: str) -> str | None: ...
    def commit(self, repository: str, sha: str) -> dict[str, Any]: ...
    def find_draft(self, repository: str, head: str, base: str) -> DraftPullRequest | None: ...
    def pull(self, repository: str, number: int) -> dict[str, Any]: ...
    def merge_checks(self, repository: str, number: int) -> dict[str, Any]: ...


class GitHubWriter(Protocol):
    scope: Literal["write"]

    def create_branch(self, repository: str, branch: str, base_sha: str) -> None: ...
    def push_files(
        self, repository: str, branch: str, base_sha: str, files: Mapping[str, str], message: str
    ) -> str: ...
    def create_draft_pull_request(
        self, repository: str, base: str, head: str, title: str, body: str
    ) -> int: ...
    def merge(self, repository: str, number: int, head_sha: str) -> str: ...


class GitHubRESTReader:
    """Fixed-origin REST reads; pagination truncation never looks like success."""

    def __init__(self, token: str = "", *, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=30, follow_redirects=False)
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": "Bearer " + token} if token else {}),
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        missing: bool = False,
    ) -> Any:
        response = self._client.request(
            method, "https://api.github.com" + path, headers=self._headers, json=data
        )
        if missing and response.status_code == 404:
            return None
        # Do not log a token, supplied request body, or arbitrary remote error text.
        if (
            400 <= response.status_code < 500
            and response.status_code not in {408, 425, 499}
            and method != "GET"
        ):
            raise DefinitiveFailure(f"GitHub rejected {method}: HTTP {response.status_code}")
        response.raise_for_status()
        if len(response.content) > 8_000_000:
            raise OSError("GitHub response exceeds the bounded delivery inventory")
        if response.headers.get("link", "").find('rel="next"') >= 0:
            raise OSError("GitHub result needs additional pages; refusing partial verification")
        return response.json()

    def _get(self, path: str, *, missing: bool = False) -> Any:
        return self._request("GET", path, missing=missing)

    def branch_sha(self, repository: str, branch: str) -> str | None:
        value = self._get(
            f"/repos/{repository}/git/ref/heads/{quote(branch, safe='')}", missing=True
        )
        return str(value["object"]["sha"]) if value else None

    def commit(self, repository: str, sha: str) -> dict[str, Any]:
        value = self._get(f"/repos/{repository}/git/commits/{sha}")
        tree = self._get(f"/repos/{repository}/git/trees/{value['tree']['sha']}?recursive=1")
        if tree.get("truncated") is not False or len(tree["tree"]) > 50_000:
            raise OSError("GitHub tree is incomplete or exceeds the delivery bound")
        return {
            "sha": value["sha"],
            "parents": [p["sha"] for p in value["parents"]],
            "tree_sha": value["tree"]["sha"],
            "files": {
                entry["path"]: {"sha": entry["sha"], "mode": entry["mode"], "type": entry["type"]}
                for entry in tree["tree"]
                if entry["type"] != "tree"
            },
        }

    def pull(self, repository: str, number: int) -> dict[str, Any]:
        return cast(dict[str, Any], self._get(f"/repos/{repository}/pulls/{number}"))

    def find_draft(self, repository: str, head: str, base: str) -> DraftPullRequest | None:
        owner = repository.split("/", 1)[0]
        pulls = self._get(
            f"/repos/{repository}/pulls?state=all&per_page=100"
            f"&head={quote(owner + ':' + head, safe='')}&base={quote(base, safe='')}"
        )
        if not pulls:
            return None
        if len(pulls) != 1:
            raise OSError("Multiple PRs match the reviewed branch")
        pull = self.pull(repository, pulls[0]["number"])
        if (
            str(pull["base"]["repo"]["full_name"]).lower() != repository.lower()
            or str(pull["head"]["repo"]["full_name"]).lower() != repository.lower()
        ):
            raise OSError("PR source or target repository differs from the approved workspace")
        return DraftPullRequest(
            number=pull["number"],
            draft=pull["draft"],
            title=pull["title"],
            body=pull["body"] or "",
            head_ref=pull["head"]["ref"],
            head_sha=pull["head"]["sha"],
            base_ref=pull["base"]["ref"],
            base_sha=pull["base"]["sha"],
            repository=str(pull["base"]["repo"]["full_name"]).lower(),
        )

    def merge_checks(self, repository: str, number: int) -> dict[str, Any]:
        pull = self.pull(repository, number)
        head = pull["head"]["sha"]
        repository_state = self._get(f"/repos/{repository}")
        checks = self._get(f"/repos/{repository}/commits/{head}/check-runs?per_page=100")
        statuses = self._get(f"/repos/{repository}/commits/{head}/status?per_page=100")
        # Mergeability includes GitHub's protection/ruleset/review evaluation.
        # Unknown/behind/blocked/unstable states never authorize a merge.
        runs = checks.get("check_runs", [])
        statuses_list = statuses.get("statuses", [])
        completed = (
            bool(runs or statuses_list)
            and all(
                run.get("status") == "completed" and run.get("conclusion") == "success"
                for run in runs
            )
            and all(item.get("state") == "success" for item in statuses_list)
        )
        if checks.get("total_count", len(runs)) != len(runs):
            completed = False
        if statuses.get("total_count", len(statuses_list)) != len(statuses_list):
            completed = False
        return {
            "repository": str(pull["base"]["repo"]["full_name"]).lower(),
            "head_repository": str(pull["head"]["repo"]["full_name"]).lower(),
            "pull_number": number,
            "head_sha": head,
            "base_sha": pull["base"]["sha"],
            "base_branch": pull["base"]["ref"],
            "draft": pull["draft"],
            "state": pull["state"],
            "merged": bool(pull.get("merged")),
            "merge_commit_sha": pull.get("merge_commit_sha"),
            "mergeable": pull.get("mergeable") is True,
            "mergeable_state": pull.get("mergeable_state"),
            "permission": repository_state.get("permissions", {}).get("push") is True,
            "ci_passed": completed,
            "checks": [
                {
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "conclusion": item.get("conclusion"),
                }
                for item in runs
            ],
            "statuses": [
                {"context": item.get("context"), "state": item.get("state")}
                for item in statuses_list
            ],
            "url": f"https://github.com/{repository}/pull/{number}",
        }


class GitHubRESTWriter(GitHubRESTReader):
    """Separate credential. Availability alone grants no publication authority."""

    scope: Literal["write"] = "write"

    def __init__(self, token: str, *, client: httpx.Client | None = None) -> None:
        if not token:
            raise DomainError("GITHUB_WRITE_CREDENTIAL_REQUIRED", "缺少独立 GitHub 写凭据。", 403)
        super().__init__(token, client=client)

    def create_branch(self, repository: str, branch: str, base_sha: str) -> None:
        self._request(
            "POST",
            f"/repos/{repository}/git/refs",
            data={
                "ref": "refs/heads/" + branch,
                "sha": base_sha,
            },
        )

    def push_files(
        self, repository: str, branch: str, base_sha: str, files: Mapping[str, str], message: str
    ) -> str:
        if self.branch_sha(repository, branch) != base_sha:
            raise DefinitiveFailure("Publication branch moved before commit creation")
        base = self.commit(repository, base_sha)
        entries = []
        for path, content in sorted(files.items()):
            old = base["files"].get(path)
            mode = old["mode"] if old else "100644"
            if mode not in {"100644", "100755"}:
                raise DefinitiveFailure("Only regular source files are publishable")
            entries.append({"path": path, "mode": mode, "type": "blob", "content": content})
        tree = self._request(
            "POST",
            f"/repos/{repository}/git/trees",
            data={
                "base_tree": base["tree_sha"],
                "tree": entries,
            },
        )
        commit = self._request(
            "POST",
            f"/repos/{repository}/git/commits",
            data={
                "message": message,
                "tree": tree["sha"],
                "parents": [base_sha],
            },
        )
        self._request(
            "PATCH",
            f"/repos/{repository}/git/refs/heads/{quote(branch, safe='')}",
            data={"sha": commit["sha"], "force": False},
        )
        return str(commit["sha"])

    def create_draft_pull_request(
        self, repository: str, base: str, head: str, title: str, body: str
    ) -> int:
        result = self._request(
            "POST",
            f"/repos/{repository}/pulls",
            data={
                "base": base,
                "head": head,
                "title": title,
                "body": body,
                "draft": True,
                "maintainer_can_modify": False,
            },
        )
        return int(result["number"])

    def merge(self, repository: str, number: int, head_sha: str) -> str:
        result = self._request(
            "PUT",
            f"/repos/{repository}/pulls/{number}/merge",
            data={
                "sha": head_sha,
                "merge_method": "merge",
            },
        )
        if result.get("merged") is not True:
            raise DefinitiveFailure("GitHub declined the explicitly approved merge")
        return str(result["sha"])
