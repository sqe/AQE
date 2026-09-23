# AQE contributor contract

Keep `main` releasable. Make the smallest change that satisfies the requested
outcome and preserve existing service, Agent Card, event, persistence, and test
catalog contracts unless the change explicitly versions them.

## Verification by change type

- Documentation-only changes: check links, examples, formatting, and workflow or
  manifest syntax touched by the change.
- Configuration changes: render or validate the affected Compose, Helm, Argo CD,
  workflow, and agent contract files without mutating a shared environment.
- Refactors: preserve observable behavior and run the full relevant unit,
  contract, regression, and integration suites.
- Features and fixes: add focused unit tests plus contract/integration coverage
  for changed boundaries. Include a negative or boundary case that would catch a
  plausible incorrect implementation.
- Agent behavior: test advertised executable skills atomically. Never invent an
  endpoint, payload, expected answer, or semantic oracle absent from the contract
  or grounded source evidence.

Use `python -m pytest`, not the `pytest` console script, so repository imports are
consistent locally and in CI. Generated tests must pass the Quality Oracle and a
real execution before entering the versioned catalog.

## Release safety

Do not bypass `CI / ci-gate`, publish from a dirty tree, move release tags, or
commit credentials or private evidence. GA tags require live model evaluation,
the disposable integration environment, telemetry checks, image publication,
and chart packaging. Production promotion uses immutable tags or digests and a
Git-reviewed rollback.
