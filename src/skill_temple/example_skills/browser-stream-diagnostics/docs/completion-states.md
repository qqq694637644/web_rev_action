# Completion states

Track four separate questions:

1. Did the transport close?
2. Did the parser reach a complete frame/value?
3. Did an application terminal marker appear?
4. Did persistent state reflect the operation?

`completed` requires the objective's evidence contract, not only a closed connection.
`partial` can support bounded claims when named evidence is complete. `interrupted`
means execution stopped before normal completion. `failed` records an error outcome.
