# Security

Please do not open a public issue for a security problem. Use **Report a vulnerability** in the Security tab of this repository to reach the maintainer privately.

What helps: the version, what an attacker could do, and steps to reproduce. You will get an answer within a week.

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
