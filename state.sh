#!/usr/bin/env bash
# Moves a finder's state between GitHub runs: encrypted (AES-256) on branch state-<name>.
# usage: STATE_KEY=... ./state.sh restore|save <name>
set -euo pipefail
cmd=$1; name=$2; branch="state-$name"
mkdir -p state
case "$cmd" in
  restore)
    if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
      git fetch -q --depth=1 origin "$branch"
      git show FETCH_HEAD:state.enc | openssl enc -d -aes-256-cbc -pbkdf2 -iter 20000 -md sha256 -pass env:STATE_KEY | tar -xz -C state
      echo "state restored"
    else
      echo "no state yet — this run seeds"
    fi ;;
  save)
    tmp=$(mktemp)
    tar -cz -C state "$name" | openssl enc -aes-256-cbc -pbkdf2 -iter 20000 -md sha256 -salt -pass env:STATE_KEY > "$tmp"
    blob=$(git hash-object -w "$tmp")
    tree=$(printf "100644 blob %s\tstate.enc\n" "$blob" | git mktree)
    commit=$(git -c user.name=finder -c user.email=finder@users.noreply.github.com commit-tree "$tree" -m "state")
    git push -q -f origin "$commit:refs/heads/$branch"
    echo "state saved" ;;
esac
