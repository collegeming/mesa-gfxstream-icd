#!/usr/bin/env bash
# CI-only bootstrap shared by bootstrap-check and fixed-baseline. Never syncs/builds.
set -euo pipefail
workspace=${1:?absolute workspace required}
lock="$workspace/patch-repo/manifests/new-fixed-44129434140"
tool="$workspace/repo-tool"
commit=97dc5c1bd9527c2abe2183b16a4b7ef037dc34a7
tag=4a375aa5f16bba000a26dd18641ebf7a99655460
# Exact annotated object provenance (not a floating tag lookup):
# https://api.github.com/repos/GerritCodeReview/git-repo/git/tags/4a375aa5f16bba000a26dd18641ebf7a99655460
# Launcher@97dc5: _Fetch L886-898 fetches heads/tags only, never detached HEAD.
# verify_rev L1060-1080 keeps the launcher's bundled-key signature verification.
test "$(git -C "$tool" rev-parse HEAD)" = "$commit"
(cd "$lock" && sha256sum -c SHA256SUMS)
printf '%s  %s\n' 9eb91b9417aec42d0159e1edd986a9e59f0bd93b078ab06203a306b8df4581b0 "$lock/resolved-manifest.xml" | sha256sum -c -
test ! -e "$workspace/aosp/.repo"
test ! -e "$workspace/locked-manifest-repo"
# Fetch only the reviewed object ID. No --no-repo-verify, fallback, or moving ref.
git -C "$tool" fetch --no-tags --depth=1 origin "$tag:refs/tags/v2.54"
test "$(git -C "$tool" rev-parse refs/tags/v2.54)" = "$tag"
test "$(git -C "$tool" cat-file -t refs/tags/v2.54)" = tag
test "$(git -C "$tool" rev-parse 'refs/tags/v2.54^{commit}')" = "$commit"
mkdir "$workspace/locked-manifest-repo"
cp "$lock/resolved-manifest.xml" "$workspace/locked-manifest-repo/default.xml"
git -C "$workspace/locked-manifest-repo" init -b main
git -C "$workspace/locked-manifest-repo" add default.xml
git -C "$workspace/locked-manifest-repo" -c user.name=crosvm-ci -c user.email=crosvm-ci@users.noreply.github.com commit -m 'Exact verified 97-project lock'
# Isolated Actions runner only; prevent repo init's interactive identity prompt.
git config --global user.name crosvm-ci
git config --global user.email crosvm-ci@users.noreply.github.com
cd "$workspace/aosp"
python3 -I "$tool/repo" init \
  -u "file://$workspace/locked-manifest-repo" -b main -m default.xml --depth 1 \
  --repo-rev="$commit"
test "$(git -C .repo/repo rev-parse HEAD)" = "$commit"
test "$(git -C .repo/repo rev-parse refs/tags/v2.54)" = "$tag"
printf 'FIXED_BOOTSTRAP_PASS repo_head=%s annotated_tag=%s; NO_PROJECT_SYNC NO_BUILD NO_DEVICE\n' "$commit" "$tag"
