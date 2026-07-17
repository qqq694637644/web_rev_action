# Credential safety

- Never copy Cookie, Authorization, CSRF, session, or private body values into chat.
- Never place captured credentials in `payload_json`.
- Identify source evidence by IDs so the backend reads local artifacts.
- Do not mutate browser-managed headers.
- Keep replay diffs redacted and evidence-local.
- When credentials are missing or expired, establish a fresh browser session and new
  source capture instead of synthesizing values.
