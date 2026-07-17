# Termination contract

Choose termination from observed protocol evidence:

- exact SSE data marker;
- bounded text pattern;
- network close;
- idle window.

Prefer the most semantic reliable marker. Use network close only when the protocol
actually ends by closing. Use an idle window only with a justified duration and record
that it is a heuristic. Verify terminal evidence after replay.
