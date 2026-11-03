# Record the vehicle profile

## Purpose

Bind every mapping to the hardware and ECU software for which evidence exists. A model name alone is insufficient: market, drivetrain, battery, production changes, and ECU calibration can alter availability, identifiers, scale, and routing.

## Inputs

- Model year, market, chassis/platform, drivetrain, engine/motor/battery variant.
- Techstream version and database region.
- ECU names and non-secret calibration/software identifiers when needed scientifically.
- Saved-session ECU keys or equivalent inventory.
- Vehicle state used during capture.

## Procedure

1. Copy [the vehicle-profile template](templates/vehicle-profile.yaml).
2. Assign an arbitrary instance alias such as `vehicle-a`; omit the VIN.
3. Separate public platform facts from observed ECU fingerprints.
4. Label each ECU as session-observed, live-address-confirmed, or merely a database candidate.
5. Record unknowns explicitly. Never imply that a database file covering several variants describes every calibration.

## Output

A profile ID referenced by every signal dossier, experiment, capture fixture, and generated catalog. Changing ECU software or testing another vehicle instance creates new evidence under a compatible or new profile; it does not silently overwrite old confidence.
