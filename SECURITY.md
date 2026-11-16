# Security

## Reporting a vulnerability

Do not open a public issue for a security problem. Report it privately through [GitHub's private vulnerability reporting](https://github.com/TazzerMAN/ha-blade/security/advisories/new).

Include the affected version, what an attacker could do, and the steps to reproduce.

## Disclosure

- You get an answer within 7 days.
- Once the vulnerability is confirmed, a fixed release and a security advisory follow within 90 days, sooner when the fix is simple.
- The advisory credits you, unless you ask otherwise.
- Please keep the vulnerability private until the advisory is published.

## Supported versions

Only the latest release gets fixes.

## Scope

- This integration stores your Blade API key in Home Assistant's configuration storage and sends it only to Blade.
- Diagnostics remove the key.
- Custom functions run with the rights of Home Assistant; native service calls are limited to entities exposed to Assist.

## How the repository is protected

- `main` only changes through pull requests with passing checks, and every commit on it is signed.
- Release tags are signed and can never be moved or deleted.
- Workflows pin every action to a commit and run with read-only permissions, except the release job.
- CodeQL, secret scanning with push protection, Dependabot alerts and the OpenSSF Scorecard run on the repository.

Problems with the Blade service itself belong to Blade.
