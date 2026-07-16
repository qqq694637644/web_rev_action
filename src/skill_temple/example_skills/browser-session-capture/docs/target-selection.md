# Target selection

Prefer observation over navigation.

- Use `page_index` when the correct tab is already known.
- Use `expected_url_contains` to guard against capturing the wrong page.
- Use `open_session.target.start_url` only when initial navigation is intentional.
- For `capture_flow`, navigation must be an explicit `navigate` step so trace and stream capture arm first.
- Record URL, title, page index, and alignment before mutating the page.

When multiple pages could match, stop and inspect rather than selecting by guesswork.
