# Sub2API Patch

`sub2api-0.1.141-optimizer.patch` is generated from:

- Base: `7cb98e5bdca776d643d284aa2f4ce7151308819e`
- Patched commit: `6d321215b652ad607ecedc3cf11b9dfdecd0f020`

It adds the safety handshake and minimal-token behavior required by targeted optimizer probes.
Run `git apply --check` against the exact base before applying it. Review and port the change
manually for any other Sub2API version.
