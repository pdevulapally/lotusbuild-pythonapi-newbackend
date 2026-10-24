import json
import urllib.error
import urllib.request


def create_pull_request(token: str, repo: str, title: str, body: str, head_branch: str, base_branch: str) -> str:
    payload = json.dumps({"title": title, "body": body, "head": head_branch, "base": base_branch}).encode()
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub PR creation failed: {exc.read().decode()}") from exc
    return data["html_url"]
