# Job polling

For a `capture_flow` or `replay_request` response with `status=running`:

1. Save the returned `experiment_id`.
2. Poll `get_experiment` with bounded intervals.
3. Stop only at `completed`, `partial`, `failed`, or `interrupted`.
4. Inspect the terminal manifest and evidence list.
5. Treat `partial` as usable only for claims supported by complete evidence entries.

Do not submit the same run operation while the original experiment remains `running`.
Use `cancel_experiment` only for an intentional stop, then poll again for terminal state.
