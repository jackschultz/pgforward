# pgforward

Task prefix: `PGF`. Tasks are `PGF-<n>`; see the workspace's
`dev/conventions/work-naming.md`.

The plan and the tasks live in the container one level up (`../README.md`,
`../roadmap.md`, Charter). The agreed design is WS-007's write-up in the
workspace records (`../README.md` has the path). Read it before changing code.

This library is public and must depend on nothing of Jack's workspace: no
`tooling/`, no other workspace package. Postgres 15+, psycopg 3.
