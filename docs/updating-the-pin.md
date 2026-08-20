# Updating the pin

`upstream.revision` is the only pin, and it belongs to the platform
repository's owner: nothing upstream reaches a project until they change it.
A platform project needs a field-guide checkout anyway — the backends are built
from its source and the Techstream scripts are run from it — so the toolkit is
installed from that checkout rather than resolved from a package index or a Git
URL. A second pin would be free to disagree with the first, and resolving one on
every run would need the network to tell you it had not changed.

Pin a release tag: `revision = "v0.3.0"`. Tags and commit hashes are both
immutable and the setup script resolves either; a branch is not a pin, because
it moves under you. The [changelog](../CHANGELOG.md) says what a bump costs —
entries marked **Breaking** require a change in the platform repository before
its commands will run again. `telemetry-validate --version` reports what is
currently installed.

[`tools/setup-platform-project.ps1`](../tools/setup-platform-project.ps1) reads
the marker and applies it: checkout, backend build, and toolkit install into a
project `.venv`. Advance the revision only after reviewing changes since the
pinned one, running platform tests, and checking the generated plan diff, then
re-run the script so both halves follow. The marker records which field-guide
revision was reviewed; it does not imply shared ancestry or a whole-repository
merge relationship.

Platform-specific programs may initially contain reusable code discovered
during experimentation. Extract that mechanism into this repository behind a
configuration or backend contract, verify parity against the platform evidence,
then leave only endpoint policy and hardware integration in the platform repo.

What a platform repository is, and what belongs on which side, is the
[platform-project contract](https://github.com/4piu/70Y074-telemetry/blob/main/docs/platform-projects.md).

