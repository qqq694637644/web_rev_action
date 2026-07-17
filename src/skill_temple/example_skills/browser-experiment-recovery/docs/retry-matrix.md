# Retry matrix

| Error/state | Dispatch started | Safe response |
|---|---:|---|
| malformed JSON or payload validation | false | Correct reported paths and retry once |
| stale operation contract | false | Reload protocol Skill and exact operation doc, then rebuild all binding fields |
| unknown operation or wrong Action | false | Read operation index and choose the correct Action |
| browser/session busy | false | Inspect owner and active experiment; wait or intentionally cancel |
| session stale/closed | false | Inspect session, then deliberately open a new or replacement session |
| operation outcome unknown | true/unknown | Do not retry; inspect current state and persistent effects |
| running job | true | Poll the same experiment to terminal state |
| interrupted/partial | true | Inspect complete evidence and decide a new independent experiment |

Never turn a network timeout into an automatic consequential retry.
