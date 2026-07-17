# Binding and mutation order

Execution order is:

1. run setup flow;
2. evaluate extractors;
3. resolve bindings;
4. apply ordered mutations;
5. dispatch one replay;
6. run verification flow;
7. compare named evidence.

Use generated bindings for volatile IDs and extractor bindings for setup-derived
values. Preserve source values only when the experiment explicitly tests them. One
experiment should isolate one causal variable.
