# Evidence selection

1. Use `list_experiments` with the narrowest session filter.
2. Select one exact `experiment_id` and inspect it with `get_experiment`.
3. Confirm terminal status and relevant quality fields.
4. Use `list_evidence` with a kind filter when possible.
5. Select stable `evidence_id` values, not array positions.
6. Use the exact reader matching the evidence kind.

For network claims, bind endpoint, request shape, response metadata, initiator, and
stream evidence to the same request evidence ID whenever possible.
