# Cancel and close order

1. Inspect `get_experiment` and `get_stream_status`.
2. If work must stop, call `cancel_experiment` for the exact session/experiment.
3. Poll `get_experiment` to `interrupted`, `partial`, `failed`, or `completed`.
4. Inspect collector cleanup and saved evidence.
5. Save any required source/evidence artifacts.
6. Call `close_session` only when no dependent inspection or replay remains.

If cancellation outcome is unknown, repeat inspection, not cancellation.
