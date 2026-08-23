#!/bin/bash
# Record that /code-review was completed for the current diff.
# Run ONLY after a genuine /code-review pass with findings resolved.
#
# The hash algorithm here MUST match require-code-review.sh exactly (SHA-256,
# `shasum -a 256`). If the two ever diverge, the marker can never match and
# every commit is blocked forever.
repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "mark-reviewed: not inside a git repository (cwd: $PWD). Run this from the same directory the commit will run in." >&2
  exit 1
}
git_dir=$(git -C "$repo_root" rev-parse --absolute-git-dir 2>/dev/null) || {
  echo "mark-reviewed: could not resolve the git directory for $repo_root." >&2
  exit 1
}
hash=$( (git -C "$repo_root" diff HEAD; git -C "$repo_root" ls-files --others --exclude-standard) | shasum -a 256 | cut -d' ' -f1)
if [ -z "$hash" ]; then
  echo "mark-reviewed: failed to hash the pending diff (is 'shasum' on PATH?). No approval recorded." >&2
  exit 1
fi
printf '%s\n' "$hash" > "$git_dir/code-review-approved" || {
  echo "mark-reviewed: could not write $git_dir/code-review-approved." >&2
  exit 1
}
echo "Code-review approval recorded for the current diff. Commit is now unblocked (until the diff changes)."
