#!/bin/sh
# Ask for ha-blade in the Marketplace catalog: one line in hacs/default/integration, from a personal fork.
# Run once, after scripts/publish.sh, when the release exists: sh scripts/catalog_pr.sh
set -eu

OWNER=TazzerMAN
SLUG="$OWNER/ha-blade"
CATALOG=hacs/default
BRANCH=add-ha-blade
TITLE="Adds new integration [$SLUG]"

fail() { echo "STOP: $*" >&2; exit 1; }

[ "$(gh api user -q .login)" = "$OWNER" ] || fail "gh is not logged in as $OWNER"
tag=$(gh release view --repo "$SLUG" --json tagName -q .tagName) || fail "no release yet: run scripts/publish.sh first"
release=$(gh release view "$tag" --repo "$SLUG" --json url -q .url)
sha=$(gh api "repos/$SLUG/commits/$tag" -q .sha)
[ -z "$(gh pr list --repo "$CATALOG" --author "$OWNER" --search "$SLUG in:title" --state all --json number -q '.[].number')" ] \
  || fail "a pull request for $SLUG already exists"

# The links must point at successful Validate jobs on the released commit.
run=$(gh run list --repo "$SLUG" --workflow Validate --commit "$sha" --status success --limit 1 --json databaseId -q '.[0].databaseId')
[ -n "$run" ] || fail "no successful Validate run on $tag"
job_url() { gh api "repos/$SLUG/actions/runs/$run/jobs" -q ".jobs[] | select(.name==\"$1\" and .conclusion==\"success\") | .html_url"; }
hacs=$(job_url hacs); hassfest=$(job_url hassfest)
[ -n "$hacs" ] && [ -n "$hassfest" ] || fail "the hacs and hassfest jobs did not both succeed on $tag"

# Personal fork, fresh branch from master.
gh repo fork "$CATALOG" --clone=false --default-branch-only >/dev/null 2>&1 || true
fork="$OWNER/default"
gh repo sync "$fork" --branch master >/dev/null
base=$(gh api "repos/$fork/git/ref/heads/master" -q .object.sha)
gh api -X POST "repos/$fork/git/refs" -f ref="refs/heads/$BRANCH" -f sha="$base" >/dev/null

# One line added, sorted the way the catalog's own sort.py sorts it.
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
gh api "repos/$fork/contents/integration?ref=$BRANCH" > "$work/file.json"
python3 - "$work" "$SLUG" <<'PY'
import base64, json, sys
work, slug = sys.argv[1:]
meta = json.load(open(f"{work}/file.json"))
before = base64.b64decode(meta["content"]).decode()
entries = json.loads(before)
if slug in entries:
    sys.exit(f"STOP: {slug} is already in the catalog")
position = sorted([*entries, slug], key=str.casefold).index(slug)
lines = before.splitlines(keepends=True)
if position == len(entries) or [json.loads(line.strip().rstrip(",")) for line in lines[1:-1]] != entries:
    sys.exit("STOP: the catalog file is not in the expected format, edit it by hand")
after = "".join([*lines[: position + 1], f'  "{slug}",\n', *lines[position + 1 :]])
if json.loads(after) != sorted([*entries, slug], key=str.casefold) or len(after.splitlines()) != len(lines) + 1:
    sys.exit("STOP: the edit is not a single sorted line")
open(f"{work}/integration", "w").write(after)
open(f"{work}/sha", "w").write(meta["sha"])
PY
gh api -X PUT "repos/$fork/contents/integration" -f message="$TITLE" -f branch="$BRANCH" \
  -f sha="$(cat "$work/sha")" -f content="$(base64 -w0 "$work/integration")" >/dev/null

cat > "$work/body.md" <<EOF
## Checklist

- [x] I've read the [publishing documentation](https://hacs.xyz/docs/publish/start).
- [x] I've added the [HACS action](https://hacs.xyz/docs/publish/action) to my repository.
- [x] (For integrations only) I've added the [hassfest action](https://developers.home-assistant.io/blog/2020/04/16/hassfest/) to my repository.
- [x] The actions are passing without any disabled checks in my repository.
- [x] I've added a link to the action run on my repository below in the links section.
- [x] I've created a new release of the repository after the validation actions were run successfully.

## Links

Link to current release: $release
Link to successful HACS action (without the \`ignore\` key): $hacs
Link to successful hassfest action (if integration): $hassfest

## Integration

Blade is an unofficial integration for [Blade](https://blade.sh), an OpenAI-compatible inference API with open-weight models on GPUs operated by Shadow in Europe. It uses the \`blade\` domain, which no core integration uses, and is set up from the UI with one API key.

It provides conversation agents with streaming, AI tasks with vision and image generation, calibrated evaluations, speech-to-text, streamed text-to-speech, and OCR, translation and forecast actions, each of which can be switched off. The repository includes local brand assets, English and French translations, an Apache-2.0 license and 359 tests at 100 % line and branch coverage.

<!-- tid:73253df5-5376-4e68-8c16-b234da6a2de3 -->
EOF
gh pr create --repo "$CATALOG" --base master --head "$OWNER:$BRANCH" --title "$TITLE" --body-file "$work/body.md"
