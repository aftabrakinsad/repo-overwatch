"""Minimal GitHub REST client for issues, pull requests, reviews and comments."""
from __future__ import annotations

import time

import requests


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API error {status}: {message}")
        self.status = status
        self.message = message


class GitHub:
    def __init__(self, token: str, repo: str, api_url: str = "https://api.github.com"):
        self.repo = repo
        self.api = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "repo-overwatch",
            }
        )

    # ------------------------------------------------------------ transport
    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        for attempt in range(5):
            response = self.session.request(method, url, timeout=60, **kwargs)
            limited = response.status_code in (403, 429) and (
                "rate limit" in response.text.lower() or response.headers.get("retry-after")
            )
            if limited and attempt < 4:
                retry_after = response.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    wait = int(retry_after)
                else:
                    reset = int(response.headers.get("x-ratelimit-reset", time.time() + 60))
                    wait = max(5, reset - int(time.time()))
                time.sleep(min(wait, 120))
                continue
            if response.status_code >= 500 and attempt < 4:
                time.sleep(2 * 2**attempt)
                continue
            if response.status_code >= 400:
                try:
                    body = response.json()
                    message = body.get("message", "")
                    details = body.get("errors")
                    if details:
                        message += f" {details}"
                except ValueError:
                    message = response.text
                raise GitHubError(response.status_code, message[:600])
            return response
        raise GitHubError(0, "retries exhausted")

    def request(self, method: str, path: str, **kwargs):
        url = path if path.startswith("http") else f"{self.api}{path}"
        response = self._send(method, url, **kwargs)
        return response.json() if response.content else None

    def paginate(self, path: str, params: dict | None = None, limit: int = 3000) -> list:
        url: str | None = f"{self.api}{path}"
        query: dict | None = {**(params or {}), "per_page": 100}
        items: list = []
        while url and len(items) < limit:
            response = self._send("GET", url, params=query)
            items.extend(response.json())
            url = response.links.get("next", {}).get("url")
            query = None  # the "next" link already carries the query string
        return items

    # ------------------------------------------------------------ labels
    def ensure_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        existing = {l["name"].lower() for l in self.paginate(f"/repos/{self.repo}/labels")}
        for name, (color, description) in labels.items():
            if name.lower() in existing:
                continue
            try:
                self.request("POST", f"/repos/{self.repo}/labels", json={"name": name, "color": color, "description": description})
            except GitHubError as exc:
                if exc.status != 422:  # 422 = already exists (race)
                    raise

    # ------------------------------------------------------------ issues
    def list_issues(self, label: str, state: str = "all") -> list[dict]:
        items = self.paginate(f"/repos/{self.repo}/issues", {"labels": label, "state": state})
        return [i for i in items if "pull_request" not in i]

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        return self.request("POST", f"/repos/{self.repo}/issues", json={"title": title, "body": body, "labels": labels})

    def update_issue(self, number: int, **fields) -> dict:
        return self.request("PATCH", f"/repos/{self.repo}/issues/{number}", json=fields)

    def comment(self, number: int, body: str) -> dict:
        return self.request("POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body})

    def list_comments(self, number: int) -> list[dict]:
        return self.paginate(f"/repos/{self.repo}/issues/{number}/comments")

    def update_comment(self, comment_id: int, body: str) -> dict:
        return self.request("PATCH", f"/repos/{self.repo}/issues/comments/{comment_id}", json={"body": body})

    # ------------------------------------------------------------ pull requests
    def find_open_pr(self, head_branch: str, base: str) -> dict | None:
        owner = self.repo.split("/")[0]
        pulls = self.request(
            "GET", f"/repos/{self.repo}/pulls",
            params={"head": f"{owner}:{head_branch}", "base": base, "state": "open"},
        )
        return pulls[0] if pulls else None

    def create_pr(self, title: str, body: str, head: str, base: str, draft: bool = False) -> dict:
        return self.request(
            "POST", f"/repos/{self.repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base, "draft": draft},
        )

    def update_pr(self, number: int, **fields) -> dict:
        return self.request("PATCH", f"/repos/{self.repo}/pulls/{number}", json=fields)

    def create_review(self, number: int, body: str, comments: list[dict]) -> dict:
        return self.request(
            "POST", f"/repos/{self.repo}/pulls/{number}/reviews",
            json={"event": "COMMENT", "body": body, "comments": comments},
        )
