# Updating the toolkit revision

The vehicle project's `telemetry-project.toml` owns `[toolkit].revision`.
Nothing in the toolkit silently advances it.

During coordinated pre-release development a project may intentionally select
`main` so integration failures surface immediately. Before publication, select
the exact reviewed release tag or commit instead.

To update safely:

1. Read `CHANGELOG.md`, especially breaking entries.
2. Ensure the managed toolkit checkout has no local edits.
3. Update `toolkit.revision` in the project marker.
4. Run `tools/setup-platform-project.ps1` from the selected toolkit checkout.
5. Verify `telemetry --version`, rebuild the private cache, inspect captures,
   compile a plan offline, and regenerate the result.
6. Review project and result diffs before committing.

Setup resolves the selected revision once, uses the same checkout for Python
and every C# worker, and refuses to move a dirty checkout. A branch selection
is intentionally moving; a release tag or commit is reproducible.
