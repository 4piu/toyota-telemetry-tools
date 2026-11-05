# Enumerations, status, and invalid values

Do not treat every bitfield as numeric. Data Lists commonly contain switches, multi-bit enumerations, validity flags, counters, and unavailable sentinels.

For each field record:

- raw-to-label enumeration and any unknown/reserved values;
- active-high/active-low semantics;
- transition threshold and hysteresis if the label represents a physical threshold;
- invalid/not-installed/not-ready/overflow encodings;
- companion validity bits and conditions in which the value freezes;
- whether a zero is physical zero or unavailable.

Validate every reachable state rather than only the default. A switch correlated with pedal contact does not prove literal pad engagement. If only correlation is available, keep the semantic confidence at Correlated and state the exact observed stimulus.

Techstream's database holds the state text it displays: a data-monitor record names a state set, and the state table maps raw values to text, including inverted switches (0 = ON) and labelled special values of numeric signals such as 0 = "None". `telemetry source import tis` carries that text as each row's `states`, and exports show it beside the formula. It is database text, not proof: a raw value missing from the set is unknown, and the observed behavior still has to agree with the label before the meaning is Correlated.

When no enum table can be recovered, publish unknown raw values rather than invented labels. Application code should carry value, timestamp, quality, and source, and mark stale/invalid data visibly.
