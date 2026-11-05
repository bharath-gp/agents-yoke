# Zoom Out Rubric

Review the implementation plan from a high-level architectural and simplicity perspective. Apply this checklist:

- **YAGNI (You Aren't Gonna Need It)**:
  - Flag features, options, flags, or configuration keys that nobody asked for.
  - Flag speculative abstractions, future-proofing, and unnecessary generalization.
  - Identify code paths or flexibility that solve hypothetical future problems.

- **Reuse of Existing Helpers and Modules**:
  - Check whether existing repo utilities, helpers, or libraries already solve the problem.
  - For each duplicate mechanism, cite the existing helper as `path:function`.
  - Prefer standard library functions and existing patterns over new implementations.

- **Simpler Alternatives**:
  - Identify whether an alternative design achieves the same goal with substantially less code or complexity.
  - Flag over-engineered data structures or complex workflows where a simple design suffices.

- **Work Item Consolidation**:
  - Identify work items that can be merged together to avoid churn or overhead.
  - Flag work items that can be completely dropped without impacting requirements.
