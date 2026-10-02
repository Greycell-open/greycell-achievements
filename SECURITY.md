# Security

Report vulnerabilities privately to the maintainers (Greycell Open) rather than
in a public issue. Include steps to reproduce.

What is designed in, and worth attacking:

- event integrity hashes, checked on read and on upload;
- per-device bearer tokens stored hashed, revocable; scrypt passwords; sign-in throttling;
- one profile per account, enforced on every sync;
- the local UI binds to 127.0.0.1, requires a per-launch token for writes, and checks the Host header;
- pack and bundle paths are resolved safely; archives are size-limited;
- platform API keys are never stored in the profile and never printed in errors.
