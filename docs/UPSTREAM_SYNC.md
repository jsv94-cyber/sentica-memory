# Upstream sync

The pin is plastic-labs/honcho v3.3.0
(`608aec8a928da13c540262ed980c569448fb53ab`). The remote name is `upstream`.

## At each upstream release

1. Read the commits and the release notes since the current pin.
2. Write a triage note: what each change fixes, which files it touches,
   whether those paths are used here, whether it conflicts with the fork,
   and a recommendation of take now, take later, or skip. Security fixes
   come first. The current note is `.review/upstream-triage.md`.
3. Bring a change across with `git cherry-pick`. Do not merge `upstream/main`.
4. A sync may be self-merged only when no change touches `src/db.py`, auth
   or tokens, the deriver queue, migrations, `src/sentica/`, or the leak
   harness. Any of those paths stops for review.

## What not to do

Do not merge the upstream branch. A merge would pull unrelated history over
the fork commits and hide which lines are ours. Cherry-picks stay one
commit per upstream change, and `FORK_CHANGES.md` stays the list of
divergences.
