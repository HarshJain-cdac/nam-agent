# Security

Report a vulnerability privately to the repository's owner
([HarshJain-cdac](https://github.com/HarshJain-cdac)), not in a public issue.
Say what is affected, how to reproduce it and what it lets an attacker do.

The NAM harness runs on NRL's own machines with their own keys. It reads no
credential for its users: authentication and the identity headers
(`X-NAM-Tenant`, `X-NAM-User`, `X-NAM-Roles`) belong to the component in
front of it.
