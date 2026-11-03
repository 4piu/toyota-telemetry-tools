# Evidence records and catalog schema

A catalog is a set of claims with provenance, not a flat list of “supported PIDs.” Keep static candidates separate from curated live-verified signals.

## Signal dossier

Copy [the signal template](../templates/signal-dossier.yaml). It records:

- stable signal/profile IDs and exact semantic/display names;
- ECU and transport addressing with endpoint confidence;
- diagnostic acquisition and any dynamic-DID source slice;
- bit layout, raw type, coefficients/expression, unit, enums and invalid states;
- independent validation points and raw/capture references;
- timing distribution, limitations, and evidence state.

## Generated catalog manifest

Every bulk extraction should include schema/tool revision, generation time, Techstream/database version/region, input filenames plus SHA-256 hashes, vehicle-profile evidence, row counts, and warnings. Do not include source databases.

Recommended separation:

- Broad generated candidates (the imported candidate list and full atlases) stay in the private project workspace when redistribution rights are unclear. Only their privacy-minimized provenance is exported.
- Reviewed records backed by sanitized evidence are exported to the deliverable repository (`telemetry export`); that exported result is the safe input to applications.
- Minimal sanitized or synthetic bytes used by tests belong with the tests in the toolkit, never raw vehicle captures.

Promote confidence only with a cited experiment. Formula confidence, endpoint confidence, semantic confidence, and transport state should remain independently representable. Never let generated code silently convert Candidate rows into production signals.
