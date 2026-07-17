# Bounded source retrieval

Use exactly one script selector: URL or script ID. Request the smallest line or offset
range that contains the relevant function, call site, or serializer.

- Expand one bounded region at a time.
- Avoid entire minified bundles.
- Record whether source maps are present or absent.
- Preserve line/offset coordinates with excerpts.
- Stop when the evidence supports or falsifies the scoped hypothesis.
