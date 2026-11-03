# Define the target signal

## Purpose

Define the physical meaning before searching names or bytes. Similar labels can represent different stages of a control system.

Record:

- exact desired quantity, unit, range, sign, and direction;
- required update rate and acceptable latency;
- operating conditions and invalid/unavailable states;
- whether a command, estimate, measured value, or proxy is acceptable;
- likely synonyms and related validity/status fields;
- an independent reference and safe physical stimulus.

For example, “brake engagement” may mean a stop-light switch, pedal stroke, requested total torque, regenerative torque, commanded hydraulic pressure, measured pressure, or literal pad-to-rotor contact. A vehicle may expose several proxies and no sensor for physical contact.

## Output

Create a draft [signal dossier](templates/signal-dossier.yaml) with `confidence: candidate` and unresolved semantic questions. This prevents a plausible formula from being mistaken for the requested concept.
