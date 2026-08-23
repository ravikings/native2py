#!/bin/bash
# PreToolUse(Bash) hook: block `git commit` unless a /code-review approval
# has been recorded (via mark-reviewed.sh) for the exact current diff.
payload=$(cat)
cmd=$(jq -r '.tool_input.command // ""' <<<"$payload")
case "$cmd" in
  *"git commit"*) ;;
  *) exit 0 ;;
esac

# Resolve the directory the command will actually run in — NOT this hook's own
# cwd. The hook runs from the session's directory (the primary checkout), but an
# agent working in a git worktree commits with `cd /path/to/worktree && git
# commit`. Resolving from the hook's cwd made it hash the primary checkout's
# diff and read the primary checkout's marker, so a worktree commit was gated on
# a completely unrelated diff — and satisfying it would have meant overwriting
# the primary checkout's approval, silently unblocking someone else's unreviewed
# work. mark-reviewed.sh has no such problem: it runs as an ordinary command in
# the agent's real cwd, so it already records into the worktree's own git dir.
run_dir=$(jq -r '.cwd // ""' <<<"$payload")
if [[ "$cmd" =~ ^[[:space:]]*cd[[:space:]]+(\"([^\"]+)\"|\'([^\']+)\'|([^[:space:];\&\|]+)) ]]; then
  cd_target="${BASH_REMATCH[2]}${BASH_REMATCH[3]}${BASH_REMATCH[4]}"
  # A relative `cd` is relative to the session cwd.
  case "$cd_target" in
    /*) run_dir="$cd_target" ;;
    *)  run_dir="${run_dir:-.}/$cd_target" ;;
  esac
fi
[ -d "$run_dir" ] || run_dir="."

repo_root=$(git -C "$run_dir" rev-parse --show-toplevel 2>/dev/null) || exit 0
# --absolute-git-dir inside a worktree yields .git/worktrees/<name>, so the
# marker is per-worktree. That is deliberate: each worktree's diff is reviewed
# and approved independently.
git_dir=$(git -C "$repo_root" rev-parse --absolute-git-dir 2>/dev/null) || exit 0
hash=$( (git -C "$repo_root" diff HEAD; git -C "$repo_root" ls-files --others --exclude-standard) | shasum | cut -d' ' -f1)
marker="$git_dir/code-review-approved"
if [ -f "$marker" ] && [ "$(cat "$marker")" = "$hash" ]; then
  exit 0
fi
cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Commit blocked: no /code-review approval recorded for the current diff. Required order: (1) git add everything you intend to commit, (2) run /code-review on the pending diff and resolve or explicitly dismiss every finding, (3) only after a genuine review, run bash .claude/hooks/mark-reviewed.sh, (4) retry the commit. If the diff changes after marking, you must review and mark again. Do NOT run mark-reviewed.sh without actually performing the review. Note: run mark-reviewed.sh from the SAME directory the commit runs in — approvals are per-worktree."}}
EOF
