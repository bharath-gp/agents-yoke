# Critic Rubric

Review the implementation plan aggressively for robustness, failure handling, and requirements coverage. Apply this checklist:

- **Failure Modes and Error Paths**:
  - Identify unhandled exceptions, failure modes, error returns, or degraded states.
  - Verify that all error paths are clearly specified and handled safely.

- **Edge Cases**:
  - Flag boundary conditions, empty inputs, malformed inputs, or extreme inputs not addressed in the plan.
  - Check for off-by-one errors, missing defaults, or invalid configuration values.

- **Concurrency and Race Conditions**:
  - Identify concurrent operations, file access races, state mutation races, or shared resource hazards.
  - Verify that locking, atomic operations, or serialization are properly defined if applicable.

- **Data Loss and Corruption**:
  - Flag potential data loss, unsafe file overwrites, unhandled crash consistency, or partial state updates.
  - Verify atomic file writes and safe state transitions.

- **Missing Requirements**:
  - Check the plan against `requirements.md` (the critic is the only lens provided with `requirements.md`).
  - Flag any requirement that is missed, incompletely addressed, or misunderstood.

- **Untestable Exit Criteria**:
  - Flag exit criteria that are subjective, ambiguous, or cannot be verified programmatically.
  - Verify that each exit criterion has concrete test commands or checks.
