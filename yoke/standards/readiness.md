# Readiness Rubric

Review the implementation plan to ensure it is fully ready for execution by a coder agent without a shell. Apply this checklist:

- **Zero-Decision Rule**:
  - A coder with no shell must not have to make any design decisions.
  - Every file path, function name, signature, config key, and test name must be explicitly given.
  - Flag any step that leaves design, naming, or architectural choices to the implementer.

- **File Boundary**:
  - Every file to be touched must be explicitly listed with its action: `create`, `modify`, or `delete`.
  - Flag any file touched during implementation that is omitted from the file list.

- **Reference Validity**:
  - All existing code, files, classes, and functions referenced must exist in the repo or be declared `create`.
  - Flag broken references, outdated function names, or missing dependencies.

- **Named Tests and Test Fixtures**:
  - Every test must be explicitly named.
  - The plan must name the fake, mock, or helper to be used for testing.
  - Flag work items that lack test definitions or test fixture specifications.
