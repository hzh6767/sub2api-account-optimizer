# Security Policy

Do not commit administrator API keys, database passwords, account credentials, logs, state, or
deployment backups. The provided Compose file reads the administrator API key from a read-only
secret file and keeps all mutation gates disabled by default.

Runtime reports contain account IDs, account names, and operational metrics. They are ignored by
Git, but should still be access-controlled and rotated on the deployment host.

Report security issues privately to the repository owner through GitHub's private vulnerability
reporting feature. Do not include live credentials or production data in a report.
