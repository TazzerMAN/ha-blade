# Contributing

Thanks for helping make Blade better in Home Assistant.

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## What a contribution needs

- You understand every line you propose and can explain it in review.
- You tested it: the suite passes at 100 % coverage, and a change in how Blade is called was also tried in a real Home Assistant.
- You answer for your code, whatever tools you used to write it.
- AI assistance is welcome under the same rules. Say in the pull request what it was used for.

## Sign-off

Every commit carries a [Developer Certificate of Origin](https://developercertificate.org) sign-off: by adding it, you certify that you have the right to submit the code under the project's license.

```sh
git commit -s
```

The line `Signed-off-by: Your Name <you@example.com>` must match the commit author. A check verifies it on every pull request, and commits made on GitHub's website are signed off automatically.

## Set up

```sh
git clone https://github.com/TazzerMAN/ha-blade
cd ha-blade
uv sync
```

The development environment pins the Home Assistant release the integration targets, with the official `pytest-homeassistant` harness.

## Before you open a pull request

```sh
uv run ruff format custom_components tests
uv run ruff check custom_components tests
uv run mypy custom_components
uv run pytest --cov
```

The suite must stay at 100 % line and branch coverage, with no network access: mock Blade, never call it.

`main` only changes through pull requests. A pull request merges, squashed into one signed commit, once the tests, hassfest, the Marketplace catalog validation, the sign-off and the title checks pass.

## Conventions

- Follow the [Home Assistant developer documentation](https://developers.home-assistant.io) and its integration quality scale; `custom_components/blade/quality_scale.yaml` lists every rule.
- Every user-facing string lives in `strings.json` and both translations. English and French are kept in step.
- Comments explain why, never what the code already says.
- Workflows pin every action to a commit and keep read-only permissions; `uvx zizmor .github/workflows` must report nothing.
- Pull request titles follow [Conventional Commits](https://www.conventionalcommits.org), such as `fix(tts): keep spaces between sentences`. The title becomes the commit on `main`, and a check enforces it.

## Releases

A release is a signed `v*` tag matching the version in `manifest.json`, with its section in `CHANGELOG.md`. The Release workflow tests the tag again and publishes the notes. Release tags can never be moved or deleted.

## Reporting a security problem

See [SECURITY.md](SECURITY.md).
