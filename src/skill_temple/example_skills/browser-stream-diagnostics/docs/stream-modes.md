# Stream modes

- **SSE:** event framing with fields such as `event`, `data`, `id`, and blank-line boundaries.
- **NDJSON:** one independent JSON value per line.
- **Raw stream:** bytes/chunks without a proven semantic framing contract.
- **Ordinary:** one bounded response body.

Classify from headers and captured bytes. Preserve an `auto` result as an observation
when evidence is insufficient; do not force a parser because the product historically streamed.
