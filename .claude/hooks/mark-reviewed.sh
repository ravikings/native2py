#!/bin/bash
# Record that /code-review was completed for the current diff.
# Run ONLY after a genuine /code-review pass with findings resolved.
repo_root=$(git rev-parse --show-toplevel) || exit 1
git_dir=$(git -C "$repo_root" rev-parse --absolute-git-dir) || exit 1
(git -C "$repo_root" diff HEAD; git -C "$repo_root" ls-files --others --exclude-standard) | shasum | cut -d' ' -f1 > "$git_dir/code-review-approved"
echo "Code-review approval recorded for the current diff. Commit is now unblocked (until the diff changes)."
