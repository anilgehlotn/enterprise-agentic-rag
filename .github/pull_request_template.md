## Summary

<!-- What this pull request does, and why, in a few lines. -->

## Changes

<!--
One bullet per change, grouped by area. Name the files that matter.
- `app/agents/nodes/grader.py`: ...
-->

-

## Testing

<!--
Paste the real output. These need no API keys:
  ruff check .
  ruff format --check .
  python -m pytest tests -q --cov=app

List anything you checked by hand (a live query, an ingestion run, a benchmark), and
anything you could not verify and why.
-->

```text

```

## Related issue

<!-- "Closes #123" for work that completes an issue, or "Refs #123" for a step towards one. -->

Closes #

## Checklist

- [ ] Lint, format and tests pass locally, with the output above
- [ ] Tests cover the new behaviour and need no API keys or network
- [ ] New environment variables are in `.env.example` and `DOCS/05_ENVIRONMENT_VARIABLES.md`
- [ ] No secrets in code, tests, docs, notebooks or workflow files
- [ ] Documentation updated where it describes what changed (README, ARCHITECTURE, DOCS)
- [ ] The `/query` response shape is unchanged, or the change is additive and the parsers are updated
- [ ] Any reported number comes from a run described in the pull request
