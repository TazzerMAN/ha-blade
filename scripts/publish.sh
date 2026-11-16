#!/bin/sh
# Publish ha-blade on GitHub: public repository, protections, first signed release.
# Run once, from the repository root, when the release is approved: sh scripts/publish.sh
set -eu

OWNER=TazzerMAN
REPO=ha-blade
SLUG="$OWNER/$REPO"
VERSION=$(python3 -c "import json; print(json.load(open('custom_components/blade/manifest.json'))['version'])")
DESCRIPTION="European AI for your home. Open-weight models on Shadow-operated GPUs in Europe, for Assist, AI tasks, speech-to-text and text-to-speech. One API key, no subscription."
TOPICS="home-assistant,home-assistant-integration,homeassistant-custom-component,blade,shadow,assist,conversation-agent,llm,speech-to-text,text-to-speech,open-weight-models,europe"

fail() { echo "STOP: $*" >&2; exit 1; }
push() { git -c credential.helper='!gh auth git-credential' push "$@"; }
latest_run() { gh run list --repo "$SLUG" --workflow "$1" --limit 1 --json databaseId -q '.[0].databaseId'; }

# Checks: nothing is published unless all of them pass.
[ -z "$(git status --porcelain)" ] || fail "uncommitted changes"
[ "$(git config user.email)" = "TazzerMAN@users.noreply.github.com" ] || fail "git identity is not TazzerMAN"
[ "$(gh api user -q .login)" = "$OWNER" ] || fail "gh is not logged in as $OWNER"
if git log --format='%B' | grep -qiE '^co-authored-by:|generated with'; then
  fail "a commit carries a co-author or tool attribution line"
fi
[ -z "$(git log --format='%G? %h' | grep -v '^G')" ] || fail "unsigned commits: $(git log --format='%G? %h' | grep -v '^G' | tr '\n' ' ')"
grep -q "Unofficial integration" README.md || fail "README lost the unofficial notice"
grep -q "^## $VERSION$" CHANGELOG.md || fail "no changelog section for $VERSION"
uv lock --check
uv run ruff check custom_components tests
uv run ruff format --check custom_components tests
uv run mypy custom_components
uv run pytest -q -p no:warnings --cov
uvx zizmor --offline .github/workflows
docker run --rm -v "$PWD:/github/workspace:ro" ghcr.io/home-assistant/hassfest:latest \
  --integration-path /github/workspace/custom_components/blade | grep -q "Invalid integrations: 0" \
  || fail "hassfest"

# Repository.
gh repo create "$SLUG" --public --description "$DESCRIPTION" --homepage "https://blade.sh" --disable-wiki
gh repo edit "$SLUG" --enable-issues --add-topic "$TOPICS" \
  --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false \
  --delete-branch-on-merge --enable-auto-merge
git remote add origin "https://github.com/$SLUG.git" 2>/dev/null || true
push -u origin main

# Security features.
gh api -X PUT "repos/$SLUG/private-vulnerability-reporting" >/dev/null
gh api -X PUT "repos/$SLUG/vulnerability-alerts" >/dev/null
gh api -X PUT "repos/$SLUG/automated-security-fixes" >/dev/null
gh api -X PATCH "repos/$SLUG" --input - >/dev/null <<'JSON'
{"security_and_analysis": {"secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "enabled"}}}
JSON
gh api -X PATCH "repos/$SLUG/code-scanning/default-setup" --input - >/dev/null <<'JSON'
{"state": "configured", "query_suite": "default"}
JSON

# Rulesets: main only changes through a squashed pull request with green checks, release tags never move.
gh api -X POST "repos/$SLUG/rulesets" --input - >/dev/null <<'JSON'
{"name": "main", "target": "branch", "enforcement": "active",
 "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
 "rules": [
  {"type": "deletion"}, {"type": "non_fast_forward"}, {"type": "required_signatures"}, {"type": "required_linear_history"},
  {"type": "pull_request", "parameters": {"required_approving_review_count": 0, "dismiss_stale_reviews_on_push": false,
    "require_code_owner_review": false, "require_last_push_approval": false, "required_review_thread_resolution": true,
    "allowed_merge_methods": ["squash"]}},
  {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": true,
    "required_status_checks": [{"context": "tests"}, {"context": "hassfest"}, {"context": "hacs"}]}}]}
JSON
gh api -X POST "repos/$SLUG/rulesets" --input - >/dev/null <<'JSON'
{"name": "release tags", "target": "tag", "enforcement": "active",
 "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
 "rules": [{"type": "deletion"}, {"type": "update"}, {"type": "non_fast_forward"}]}
JSON

# Proof that every protection is on.
gh api "repos/$SLUG" -q '.security_and_analysis | to_entries[] | "\(.key): \(.value.status)"'
gh api "repos/$SLUG/code-scanning/default-setup" -q '"code scanning: \(.state)"'
gh api "repos/$SLUG/rulesets" -q '.[] | "ruleset: \(.name) (\(.enforcement))"'
[ "$(gh api "repos/$SLUG/rulesets" -q 'length')" = 2 ] || fail "rulesets missing"

echo "Waiting for the Validate and Tests workflows on main..."
sleep 20
gh run watch --repo "$SLUG" --exit-status "$(latest_run Validate)"
gh run watch --repo "$SLUG" --exit-status "$(latest_run Tests)"

# The signed tag starts the Release workflow, which tests again and publishes the release notes.
git tag -s "v$VERSION" -m "v$VERSION"
push origin "v$VERSION"
sleep 20
gh run watch --repo "$SLUG" --exit-status "$(latest_run Release)"
gh release view "v$VERSION" --repo "$SLUG" --json url -q .url

echo "Published: https://github.com/$SLUG"
echo "By hand (no API for it): Settings > Social preview > upload docs/images/social-preview.png"
echo "Next: sh scripts/catalog_pr.sh asks for the Marketplace catalog (reviews take months)."
