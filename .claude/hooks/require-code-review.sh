#!/bin/bash
# PreToolUse(Bash) hook: block `git commit` unless a /code-review approval
# has been recorded (via mark-reviewed.sh) for the exact current diff.
#
# This script's ONLY job is to block. Silently not-blocking is its worst
# possible failure, so every path that cannot establish "this diff was
# reviewed" must DENY, never fall through to exit 0. The single allow path is
# the marker matching the diff hash; the other is "this is not a commit at all".

# Emit a PreToolUse deny decision with the given reason and stop.
# Deliberately pure bash: a script whose job is to block must not itself depend
# on jq/python/tr being present in order to say "no".
deny() {
  local reason="$1"
  reason="${reason//\\/}"        # drop backslashes
  reason="${reason//\"/\'}"      # double quotes -> single, keeps the JSON valid
  reason="${reason//$'\n'/ }"    # no raw newlines inside a JSON string
  reason="${reason//$'\t'/ }"
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$reason"
  exit 0
}

UNREVIEWED_REASON='Commit blocked: no /code-review approval recorded for the current diff. Required order: (1) git add everything you intend to commit, (2) run /code-review on the pending diff and resolve or explicitly dismiss every finding, (3) only after a genuine review, run bash .claude/hooks/mark-reviewed.sh, (4) retry the commit. If the diff changes after marking, you must review and mark again. Do NOT run mark-reviewed.sh without actually performing the review. Note: run mark-reviewed.sh from the SAME directory the commit runs in — approvals are per-worktree.'

payload=$(cat)

# Parse the command. If the payload cannot be parsed at all (no jq, malformed
# JSON), we do NOT know whether this is a commit — so fail closed for anything
# whose raw payload even mentions a commit, rather than waving it through.
if command -v jq >/dev/null 2>&1; then
  cmd=$(jq -r '.tool_input.command // ""' <<<"$payload" 2>/dev/null)
  parse_rc=$?
else
  parse_rc=127
fi
if [ "$parse_rc" -ne 0 ]; then
  case "$payload" in
    *commit*)
      if command -v jq >/dev/null 2>&1; then
        deny "Commit blocked: this hook could not parse its input payload as JSON, so it cannot tell whether this command is a commit or whether the current diff was reviewed. Failing closed. Re-run the command, or fix the hook payload."
      fi
      deny "Commit blocked: the required-code-review hook needs \`jq\` and it is not on PATH, so it cannot inspect the command or verify a review. Failing closed. Install jq (e.g. brew install jq / apt-get install jq) and retry."
      ;;
    *) exit 0 ;;
  esac
fi

# Match `git commit` as an actual command invocation, not as a substring
# anywhere in the text. A bare `*"git commit"*` glob also fired on commands that
# merely MENTION it -- `grep -r "git commit" docs/`, an echo, or a test harness
# feeding payloads to this very hook -- which blocked ordinary work with a
# message about reviewing a diff the command was never going to touch.
#
# The command must therefore sit at a shell command boundary: start of string,
# or after ; && || | & or an opening paren. That still catches every real form,
# including the worktree idiom `cd /path/to/wt && git commit -m x` and
# `git -c user.email=x commit`, while ignoring the same words inside a quoted
# argument (which is always preceded by a quote, not a separator).
#
# Deliberately still biased toward blocking: this is a commit gate, so a false
# positive costs an argument, while a false negative lets unreviewed work
# through. A command that hides the commit behind a wrapper (`bash -c "git
# commit"`, `xargs git commit`) is NOT caught -- accepted, because catching it
# would mean re-blocking every mention.
if ! printf '%s' "$cmd" | grep -Eq '(^|[;&|(]|&&|\|\|)[[:space:]]*git([[:space:]]+-[^[:space:]]+([[:space:]]+[^[:space:]]+)?)*[[:space:]]+commit\b'; then
  exit 0
fi

# Resolve the directory the command will actually run in — NOT this hook's own
# cwd. The hook runs from the session's directory (the primary checkout), but an
# agent working in a git worktree commits with `cd /path/to/worktree && git
# commit`. Resolving from the hook's cwd made it hash the primary checkout's
# diff and read the primary checkout's marker, so a worktree commit was gated on
# a completely unrelated diff — and satisfying it would have meant overwriting
# the primary checkout's approval, silently unblocking someone else's unreviewed
# work. mark-reviewed.sh has no such problem: it runs as an ordinary command in
# the agent's real cwd, so it already records into the worktree's own git dir.
run_dir=$(jq -r '.cwd // ""' <<<"$payload" 2>/dev/null)
if [[ "$cmd" =~ ^[[:space:]]*cd[[:space:]]+(\"([^\"]+)\"|\'([^\']+)\'|([^[:space:];\&\|]+)) ]]; then
  cd_target="${BASH_REMATCH[2]}${BASH_REMATCH[3]}${BASH_REMATCH[4]}"
  # A relative `cd` is relative to the session cwd.
  case "$cd_target" in
    /*) run_dir="$cd_target" ;;
    *)  run_dir="${run_dir:-.}/$cd_target" ;;
  esac
fi

# Previously an unusable run_dir silently fell back to "." — which hashed some
# other checkout's diff, or fell through to exit 0. Both are fail-open. Deny.
if [ -z "$run_dir" ]; then
  deny "Commit blocked: this hook could not determine which directory the commit would run in (the payload carried no cwd), so it cannot check that directory's diff against a recorded /code-review approval. Failing closed."
fi
if [ ! -d "$run_dir" ]; then
  deny "Commit blocked: the directory this commit would run in does not exist or is not a directory: '$run_dir'. The hook cannot verify a /code-review approval for it, so it is failing closed. Fix the path in your command (the leading \`cd\`) and retry."
fi

repo_root=$(git -C "$run_dir" rev-parse --show-toplevel 2>/dev/null) || \
  deny "Commit blocked: '$run_dir' is not inside a git repository, so this hook cannot compute the pending diff or find a /code-review approval marker. The commit would fail there anyway. Run the commit from inside the repository (or the worktree) you mean to commit to."
git_dir=$(git -C "$repo_root" rev-parse --absolute-git-dir 2>/dev/null) || \
  deny "Commit blocked: git could not resolve the git directory for '$repo_root', so this hook cannot read or write the /code-review approval marker. Failing closed. Check the repository is not corrupt and that you can run 'git -C $repo_root status'."

# --absolute-git-dir inside a worktree yields .git/worktrees/<name>, so the
# marker is per-worktree. That is deliberate: each worktree's diff is reviewed
# and approved independently.
hash=$( (git -C "$repo_root" diff HEAD; git -C "$repo_root" ls-files --others --exclude-standard) | shasum -a 256 | cut -d' ' -f1)
if [ -z "$hash" ]; then
  deny "Commit blocked: this hook could not hash the pending diff (is \`shasum\` on PATH, and does 'git -C $repo_root diff HEAD' work?). Without a hash it cannot match a /code-review approval, so it is failing closed."
fi

marker="$git_dir/code-review-approved"
if [ -f "$marker" ] && [ "$(cat "$marker" 2>/dev/null)" = "$hash" ]; then
  exit 0
fi
deny "$UNREVIEWED_REASON"
