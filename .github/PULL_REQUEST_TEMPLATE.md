## What & why

Describe the change and the user-visible effect.

## Checklist

- [ ] `pytest` passes
- [ ] `leptin bench` still passes (≥60% reduction at ≤2% recall loss)
- [ ] Added/updated tests (mapped to a PRD acceptance criterion where one exists)
- [ ] Kept the core dependency-free (new deps behind an optional extra)
- [ ] Did not weaken the recall guardrail or fold footprint reductions into the
      headline `tokens_saved`

## Notes for reviewers
