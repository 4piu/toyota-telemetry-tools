# Supporting extraction and setup tools

The installed Python command surface is the single `telemetry` dispatcher; see
the repository [README](../README.md) and [quickstart](../docs/quickstart.md).

This directory contains repository/setup scripts and offline extraction tools
that operate before normalized catalog rows enter a vehicle project:

- `setup-platform-project.ps1` selects the toolkit revision declared by a
  schema-2 project, builds private adapter workers, and installs Python;
- `catalog/` turns an already extracted OEM database into normalized candidate
  rows;
- `techstream/` queries locally installed source data;
- `j2534/` parses recorded pass-through logs offline.

OEM databases, vendor libraries, raw logs, and complete derived atlases are
private inputs. Keep them under the vehicle project's ignored `scratch/`
boundary and commit only reviewed provenance and privacy-minimized results.
