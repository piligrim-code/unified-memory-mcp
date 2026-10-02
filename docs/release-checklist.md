# Release Gate

Candidate: 3.0.0rc1. Do not label as a stable production release solely from CI.

- Review the exact public commit and diff. Do not copy private workstation data.
- Build wheel and sdist from a clean checkout. Inspect names and content for
  credentials, databases, local paths, raw logs and unintended modules.
- Install the wheel outside the source directory; assert import locations and
  run the synthetic two-client demo plus doctor on its disposable database.
- Run source regressions on Windows/Linux and Python 3.11, 3.12, 3.13. Optional
  embeddings and vendor-client integrations require separate qualification.
- Verify old-database upgrade, stale-write conflicts, backup/restore and failure
  recovery. Keep rollback backup/version instructions with the release.
- Record commit, artifact SHA-256, environment and exact test counts, including
  skips and failures investigated. A transitive dependency lock is not claimed.
- Review changelog, support matrix, content retention and private vulnerability
  reporting channel. Do not advertise a reporting channel before it is enabled.
- Publish package/tag/release only as a separate authorized action.

The supported default is a local same-owner stdio server with SQLite/FTS and no
mandatory third-party Python runtime dependencies. HTTP gateway, optional models,
network filesystems, mutually untrusted tenants and automatic private imports
are not part of the continuity qualification.

`SOURCE_MANIFEST.json` and `SHA256SUMS.txt` document the original public import at
commit 64d3a9e, not current release checksums. Do not use those historical hashes
to attest this candidate; build artifacts and their fresh hashes are the evidence.

Support policy: reproduce bugs with minimal synthetic inputs. Prioritize data
loss, privacy and concurrency regressions over features. No response-time SLA
or external security certification is promised. Review ordinary issues weekly
when maintaining the project; publish any support-policy change explicitly.
