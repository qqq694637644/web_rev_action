# Current-site report contract

Write only derived reports. Do not copy credential artifacts, raw headers, raw request or response bodies, cookies, tokens, session IDs, or private screenshots into reports.

## Required reports

### `current-site-inventory.md`

Include:

```text
analysis question
session and analysis series
current page facts
relevant experiments
observed UI and state transitions
known limitations
```

### `current-ui-map.md`

Map visible controls and state transitions to exact experiment and evidence IDs. Mark unverified persistence or reload behavior as unknown.

### `current-network-map.md`

For each relevant request include:

```text
method + origin/path
exact evidence or observation ID
query parameter names, not values
request and response content types
HTTP status, when observed
request lifecycle status, when observed
transport observation
initiator/source references
association confidence
completeness gaps
```

Never substitute lifecycle values such as `finished` for HTTP status.

### `open-questions.md`

List unresolved facts, why existing evidence is insufficient, and the smallest next experiment. Separate missing evidence from ambiguous association.

## Optional reports

Create only when supported by current evidence:

```text
request-schema.json
response-schema.json
stream-event-catalog.md
source-map.md
state-transition-map.md
replay-notes.md
```

## Claim format

Every important claim should include:

```text
claim
observation type: direct | derived comparison | hypothesis
experiment ID
evidence ID or observation ID
artifact ID when relevant
completeness or association caveat
```

## Comparison language

Use narrow language:

- `response_status equivalent` means the selected HTTP status values match;
- `response_content_type equivalent` means only Content-Type matches;
- `stream_summary equivalent` means event counts, terminal reason, and primary event source match;
- it does not mean response headers, stream data, framing, or business semantics are fully equivalent.

## Completion check

Before finalizing:

- every endpoint claim points to exact evidence;
- HTTP status and request lifecycle status are distinct;
- stream summaries use one canonical shape on both sides;
- missing and ambiguous facts remain explicit;
- current-site facts drive the experiment sequence;
- product-specific scenarios appear only when observed on the current site;
- no private artifact content or credential value appears in the report.
