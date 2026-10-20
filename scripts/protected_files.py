"""Run only from trusted main; inspect PR metadata without executing PR code."""

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

from risk_tier import classify, load_rules, markdown_summary


APPROVAL_LABEL = "human-approved"
APPROVAL_CONTEXT = "protected-files/human-approval"


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository")
        self.root = f"https://api.github.com/repos/{repository}"
        self.token = token

    def request(self, method, path, data=None):
        request = urllib.request.Request(
            self.root + path,
            method=method,
            data=json.dumps(data).encode("utf-8") if data is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            if method == "DELETE" and error.code == 404:
                return None
            raise RuntimeError(f"GitHub API failed: HTTP {error.code}") from None


def head_has_human_approval(api, sha, context):
    # The list endpoint includes creator; combined status omits that field.
    for page in range(1, 31):
        statuses = api.request("GET", f"/commits/{sha}/statuses?per_page=100&page={page}")
        if not isinstance(statuses, list):
            raise RuntimeError("Invalid commit-status response")
        for status in statuses:
            if status["context"].lower() == context:
                return (
                    status["state"] == "success"
                    and status.get("creator", {}).get("login") == "github-actions[bot]"
                )
        if len(statuses) < 100:
            return False
    raise RuntimeError("Approval status history is incomplete; cannot approve")


def inspect_pull_request(api, event, rules):
    number = int(event["number"])
    approval_context = f"{APPROVAL_CONTEXT}/pr-{number}"
    expected_head = event["pull_request"]["head"]["sha"]
    current = api.request("GET", f"/pulls/{number}")
    action = event["action"]
    approval_event = event.get("label", {}).get("name") == APPROVAL_LABEL
    if action == "synchronize":
        # Revocation must run even for a stale event or temporarily changed base.
        api.request("DELETE", f"/issues/{number}/labels/{urllib.parse.quote(APPROVAL_LABEL)}")
    if action == "unlabeled" and approval_event:
        api.request("POST", f"/statuses/{expected_head}", {
            "context": approval_context,
            "state": "failure",
            "description": "Human approval revoked; review the current Head.",
        })
    if current["head"]["sha"] != expected_head:
        raise RuntimeError("PR Head changed; wait for the new Head's check")
    if current["base"]["ref"] != "main":
        raise RuntimeError("This merge guard requires a PR targeting main")
    if action == "labeled" and approval_event:
        if event.get("sender", {}).get("type") != "User":
            raise RuntimeError("Human approval must be a user label event, not an automation")
        api.request("POST", f"/statuses/{expected_head}", {
            "context": approval_context,
            "state": "success",
            "description": "Human label approved this exact Head.",
        })

    files = []
    for page in range(1, 31):
        batch = api.request("GET", f"/pulls/{number}/files?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise RuntimeError("Invalid changed-file response")
        files.extend(batch)
        if len(batch) < 100:
            break
    if len(files) != current["changed_files"]:
        raise RuntimeError("Incomplete changed-file list; cannot approve")
    paths = []
    for item in files:
        paths.append(item["filename"])
        if item.get("previous_filename"):
            paths.append(item["previous_filename"])
    result = classify(paths, rules)

    latest = api.request("GET", f"/pulls/{number}")
    if latest["head"]["sha"] != expected_head or latest["base"]["sha"] != current["base"]["sha"]:
        raise RuntimeError("PR Head/base changed while checking; rerun on the new combination")
    labels = {label["name"] for label in latest["labels"]}
    head_approved = head_has_human_approval(api, expected_head, approval_context)
    approved = APPROVAL_LABEL in labels and head_approved and action != "synchronize"
    result.update({
        "head": expected_head,
        "base": latest["base"]["sha"],
        "approved": approved,
        "allowed": result["tier"] != "L2" or approved,
    })
    return result


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    result = inspect_pull_request(api, event, load_rules())
    summary = markdown_summary(result)
    summary += f"\nHead: `{result['head']}`; base: `{result['base']}`.\n"
    summary += f"\nHuman approval: {'present' if result['approved'] else 'absent / revoked'}.\n"
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
        output.write(summary)
    print(f"Risk {result['tier']}; {len(result['matches'])} paths; allowed={result['allowed']}")
    if not result["allowed"]:
        raise SystemExit("L2 requires Human approval of this Head via human-approved")


if __name__ == "__main__":
    main()
