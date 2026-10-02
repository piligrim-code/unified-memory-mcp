# Voluntary Pilot Checklist

Status: prepared, not executed. No recruited users or retention figures claimed.
Aim: five independent installations; proposed early signal is four successful
installs and three people returning on another day within two weeks. These are
exploratory product criteria, not statistical proof of market demand.

## Participant Boundaries

- Use a separate environment and a new synthetic database first.
- Explain that this is a breaking-contract release candidate, not a silent
  upgrade of existing assistant memory. Never mix v2 writers with a v3 store.
- Participation and feedback are voluntary; no background telemetry is added.
- Do not submit database files, tokens, real transcripts, private project names,
  raw diagnostic archives or model prompts to public issues.
- Stop testing if the tool selects the wrong project/task, unexpectedly changes
  a record, requests private data or makes an unexpected external connection.

## Tasks

1. Install from a named candidate commit/artifact and connect one client using
   only the public instructions; record time and points requiring assistance.
2. Save a synthetic decision and task checkpoint. Restart and recover it.
3. Connect a second client; select the same task and inspect source/revision.
4. Create another task under the same project; verify explicit selection and
   refusal to guess an ambiguous task.
5. Trigger a stale write; reread/reconcile without losing the newer checkpoint.
6. Run doctor, backup/restore to a new path, and an export/import preview.
7. Return on a different day, only if the workflow was useful. Compare with
   plain handoff notes; report friction and wrong-context incidents, not merely
   the number of saved memories.

## Minimal Feedback

Share only: candidate commit/version, OS, Python/client version, task number,
pass/fail, setup minutes, whether help was needed, number of wrong-context or
lost-update incidents, and an optional synthetic reproducer. Describe the useful
workflow in generic terms. No secret-containing attachments are necessary.

Maintainer actions: reproduce failures with synthetic data, prioritize data
loss/privacy/wrong-task issues, fix instructions before adding features, and
record actual repeat use separately from stated interest. If usage is weak,
narrow the workflow rather than adding a hosted platform.
