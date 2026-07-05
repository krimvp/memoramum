#!/usr/bin/env python3
"""PostToolUse hook: validate relative links and anchors in an edited Markdown file.

Invoked two ways:
  - as a Claude Code PostToolUse hook (reads the tool payload from stdin); exits 2 with
    findings on stderr so the agent sees and fixes them immediately;
  - manually, via CLAUDE_HOOK_CHECK_FILE=<path> (used by the /docs-check skill); exits 1
    with findings on stdout.

Checks, for every inline link [text](target) in the file:
  - http(s)/mailto links: skipped;
  - relative file targets: the file must exist (resolved against the linking file's dir);
  - #anchors (same-file or on a relative target): a heading with that GitHub slug must
    exist in the target file.
"""
import json
import os
import re
import sys

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
FENCE_RE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1\s*$", re.MULTILINE | re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def blank_out(match: re.Match) -> str:
    """Replace a match with whitespace, preserving newlines so line numbers hold."""
    return re.sub(r"[^\n]", " ", match.group(0))


def github_slug(heading: str) -> str:
    h = heading.strip().lower()
    h = re.sub(r"[^\w\s-]", "", h)      # drop punctuation (keeps word chars, spaces, hyphens)
    return re.sub(r"\s", "-", h)        # every space becomes a hyphen (not collapsed)


def anchors_of(path: str) -> set:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return set()
    return {github_slug(m.group(1)) for m in HEADING_RE.finditer(text)}


def check_file(md_path: str) -> list:
    problems = []
    try:
        with open(md_path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return [f"{md_path}: unreadable ({e})"]

    # Example links inside code fences / inline code are not real links.
    text = FENCE_RE.sub(blank_out, text)
    text = INLINE_CODE_RE.sub(blank_out, text)

    base_dir = os.path.dirname(os.path.abspath(md_path))
    for m in LINK_RE.finditer(text):
        target = m.group(1)
        line = text.count("\n", 0, m.start()) + 1
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):   # http:, https:, mailto:, …
            continue
        path_part, _, anchor = target.partition("#")
        if path_part:
            resolved = os.path.normpath(os.path.join(base_dir, path_part))
            if not os.path.exists(resolved):
                problems.append(f"{md_path}:{line}: broken link → {target} (no such file: {path_part})")
                continue
            anchor_file = resolved
        else:
            anchor_file = md_path
        if anchor and anchor_file.endswith(".md"):
            if anchor not in anchors_of(anchor_file):
                problems.append(f"{md_path}:{line}: broken anchor → {target} (no heading slugs to '#{anchor}' in {os.path.relpath(anchor_file)})")
    return problems


def main() -> int:
    manual = os.environ.get("CLAUDE_HOOK_CHECK_FILE")
    if manual:
        problems = check_file(manual)
        for p in problems:
            print(p)
        return 1 if problems else 0

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    file_path = (payload.get("tool_input") or {}).get("file_path", "")
    if not file_path.endswith(".md") or not os.path.exists(file_path):
        return 0
    problems = check_file(file_path)
    if problems:
        print("Markdown link check failed — fix these before committing:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
