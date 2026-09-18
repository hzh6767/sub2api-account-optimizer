# Sub2API Patch

`sub2api-0.2.5-optimizer.patch` is generated for:

- Version: `0.2.5` (`backend/cmd/server/VERSION`)
- Scope: account-test `mode=optimizer` handshake and minimal-token SSE probe

It adds the safety handshake and minimal-token behavior required by targeted optimizer probes.
Run `git apply --check` against the exact base before applying it. Review and port the change
manually for any other Sub2API version. The older `sub2api-0.1.141-optimizer.patch` file is retained as a historical archive only.
