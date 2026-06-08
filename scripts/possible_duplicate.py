#!/usr/bin/env python3
"""Flag open PRs that appear to overlap (touch the same files).

The open-PR backlog accumulates silent duplicates — several contributors
independently fixing the same thing — which costs maintainers real triage
time. This script, run in CI on each PR, compares the PR's changed files
against the other open PRs and, when there's meaningful overlap, leaves a
single sticky comment + a `possible-duplicate` label so the duplication is
visible *before* anyone invests review effort.

Design notes:
  * Pure standard library (urllib) — no pip install needed in CI.
  * Read-only against other PRs; the only writes are one sticky comment and
    one label on the *triggering* PR. It self-heals: if a later push removes
    the overlap, the comment + label are cleared.
  * It is a heuristic (file overlap), so it is framed as "possible" and is
    easy to dismiss. Thresholds are env-tunable.
  * Run locally with DRY_RUN=1 to print results without writing anything:
      GITHUB_TOKEN=$(gh auth token) GITHUB_REPOSITORY=owner/repo \
        PR_NUMBER=123 DRY_RUN=1 python scripts/possible_duplicate.py

Limitations (by design, stated honestly):
  * Compares against the MAX_PRS most-recently-updated open PRs, not the whole
    backlog — fetching files for all ~hundreds of open PRs every run would
    exceed the GITHUB_TOKEN rate limit (1000 req/hr). So a brand-new PR that
    duplicates a *stale* one beyond the window can be missed. Raise MAX_PRS for
    more coverage; full-backlog coverage would want a cached file index (v2).
  * Only flags overlap on *rare* files. Two PRs that overlap only in hub files
    (app.py, core/database.py) are intentionally NOT flagged — that overlap is
    almost always coincidental, not duplicate work.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

API = "https://api.github.com"
REPO = os.environ["GITHUB_REPOSITORY"]
PR = int(os.environ["PR_NUMBER"])
TOKEN = os.environ.get("GITHUB_TOKEN", "")
DRY = os.environ.get("DRY_RUN") == "1"

# Tunables (overridable via env in the workflow).
MAX_PRS = int(os.environ.get("MAX_PRS", "200"))     # candidate cap (most-recently-updated; rate-limit bound)
MIN_SHARED = int(os.environ.get("MIN_SHARED", "2"))  # discriminative shared files to flag...
VERY_RARE = int(os.environ.get("VERY_RARE", "4"))    # ...unless one shared file is this rare (PR count)
JAC_FLOOR = float(os.environ.get("JAC_FLOOR", "0.08"))  # multi-file matches need at least this overlap
MARKER = "<!-- possible-duplicate-bot -->"

# Files too ubiquitous to be a useful signal on their own (sharing only these
# is almost always coincidental, e.g. two PRs each adding a README row).
_COMMON = {
    "readme.md", ".env.example", "requirements.txt", "package.json",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "pyproject.toml",
    ".gitignore", "changelog.md", "dockerfile", "docker-compose.yml",
}


def _req(method, path, data=None, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def _paginate(path, params=None, cap=1000):
    params = dict(params or {})
    params["per_page"] = 100
    out, page = [], 1
    while len(out) < cap:
        params["page"] = page
        batch = _req("GET", path, params=params)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out[:cap]


def _pr_files(num):
    return {f["filename"] for f in _paginate(f"/repos/{REPO}/pulls/{num}/files", cap=300)}


def _find_bot_comment():
    for c in _paginate(f"/repos/{REPO}/issues/{PR}/comments", cap=200):
        if MARKER in (c.get("body") or ""):
            return c
    return None


def _ensure_label():
    try:
        _req("GET", f"/repos/{REPO}/labels/possible-duplicate")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _req("POST", f"/repos/{REPO}/labels", {
                "name": "possible-duplicate",
                "color": "fbca04",
                "description": "Appears to overlap another open PR (auto-flagged)",
            })


def _clear(existing):
    """Self-heal: remove a stale flag when the overlap is gone."""
    if not existing:
        return
    try:
        _req("DELETE", f"/repos/{REPO}/issues/comments/{existing['id']}")
    except urllib.error.HTTPError:
        pass
    try:
        _req("DELETE", f"/repos/{REPO}/issues/{PR}/labels/possible-duplicate")
    except urllib.error.HTTPError:
        pass


def _render(matches):
    lines = [
        MARKER,
        "### 🔁 Possible duplicate / overlap",
        "",
        "This PR touches the same files as the open PR(s) below, so at most one of "
        "each can merge cleanly. Flagging it so duplication is visible before review "
        "effort goes in — this is an automated heuristic and may be wrong.",
        "",
        "| Overlapping PR | Shared files | Overlap |",
        "|---|---|---|",
    ]
    for num, title, files, jac in matches[:10]:
        shown = ", ".join(f"`{f}`" for f in files[:5]) + (" …" if len(files) > 5 else "")
        lines.append(f"| #{num} — {title[:60]} | {shown} | {int(jac * 100)}% |")
    lines += [
        "",
        "<sub>Heuristic file-overlap check. Remove the `possible-duplicate` label "
        "to dismiss; tune with `MIN_SHARED` / `MIN_JACCARD`.</sub>",
    ]
    return "\n".join(lines)


def main():
    mine = _pr_files(PR)
    if not mine:
        print("This PR changes no files; nothing to compare.")
        return
    candidates = _paginate(
        f"/repos/{REPO}/pulls",
        params={"state": "open", "sort": "updated", "direction": "desc"},
        cap=MAX_PRS,
    )

    # Fetch each candidate's files once, then weight shared files by RARITY.
    # A file touched by many open PRs (app.py, core/database.py) is a hub and
    # carries no duplicate signal; a file touched by only 2-3 PRs (e.g. a new
    # email_oauth.py) is a strong one. This is what separates real duplicates
    # from PRs that merely both edit a central file.
    others = {}
    for o in candidates:
        if o["number"] == PR or o.get("draft"):
            continue
        others[o["number"]] = (o["title"], _pr_files(o["number"]))

    pop = Counter()
    for f in mine:
        pop[f] += 1
    for _title, files in others.values():
        for f in files:
            pop[f] += 1
    # A file is a "hub" (ignored) if it appears in more than ~6% of the
    # compared PRs, floored at 8.
    hub_max = max(8, int(0.06 * (len(others) + 1)))

    def discriminative(f):
        base = os.path.basename(f).lower()
        return pop[f] <= hub_max and f.lower() not in _COMMON and base not in _COMMON

    scored = []
    for num, (title, files) in others.items():
        shared = mine & files
        disc = sorted(f for f in shared if discriminative(f))
        if not disc:
            continue
        rarest = min(pop[f] for f in disc)
        jac = len(shared) / len(mine | files)
        # Flag on a single very-rare shared file (strong signal regardless of
        # size), OR enough discriminative shared files with real overlap.
        if rarest <= VERY_RARE or (len(disc) >= MIN_SHARED and jac >= JAC_FLOOR):
            scored.append((num, title, disc, round(jac, 3), rarest))

    # strongest first: most discriminative shared files, then rarest, then overlap
    scored.sort(key=lambda m: (-len(m[2]), m[4], -m[3]))
    matches = [(n, t, f, j) for (n, t, f, j, _r) in scored]

    existing = None if DRY else _find_bot_comment()
    if not matches:
        print("No overlapping open PRs found.")
        _clear(existing)
        return

    body = _render(matches)
    if DRY:
        print(f"[DRY_RUN] would flag {len(matches)} overlap(s) on PR #{PR}:\n")
        print(body)
        return

    if existing:
        _req("PATCH", f"/repos/{REPO}/issues/comments/{existing['id']}", {"body": body})
    else:
        _req("POST", f"/repos/{REPO}/issues/{PR}/comments", {"body": body})
    _ensure_label()
    _req("POST", f"/repos/{REPO}/issues/{PR}/labels", {"labels": ["possible-duplicate"]})
    print(f"Flagged {len(matches)} possible duplicate(s) on PR #{PR}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never block a PR on the dup-checker failing
        print(f"possible-duplicate: skipped due to error: {exc}", file=sys.stderr)
        sys.exit(0)
