# Locate acquisition metadata

Use the least expensive source that answers the current question; combine routes for stronger evidence.

| Route | Best when | Produces |
|---|---|---|
| [J2534 capture](j2534-capture.md) | Techstream and a VIM work | Exact API conversation and timing |
| [Techstream database](techstream-database.md) | Local databases are installed | Broad names, identifiers, fields, conversions |
| [Saved session](saved-session.md) | A `.TSE` exists | Vehicle-evidenced ECU/database keys |
| [Static analysis](static-analysis.md) | A narrow structure or call path remains ambiguous | A focused implementation fact |

First locate the exact Techstream Data List name, its ECU/menu, unit, related flags, and duplicates. A duplicate in an ECU already being polled may be more useful than the conceptually primary source.

The output of this stage is Candidate evidence. A database record or decompiled branch is not proof that the connected calibration accepts the request.
