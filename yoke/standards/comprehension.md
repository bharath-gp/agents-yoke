# Comprehension Rubric

Review the implementation plan as a naive reader who has access only to `plan.md` and the existing repository code. Apply this checklist:

- **Paraphrase**:
  - First, paraphrase what the plan proposes to build and what exists in the system today.
  - Capture this initial understanding in the `paraphrase` object before evaluating findings.

- **Undefined Terms**:
  - Flag terms, concepts, acronyms, or identifiers used without definition or explanation.
  - Flag concepts that are not defined in the plan or codebase.

- **Unstated Context**:
  - Flag steps or design choices that assume context or knowledge not explicitly stated in the plan.
  - Verify that a reader without external memory can understand every instruction.

- **Contradictions Between Sections**:
  - Check for conflicting requirements, interfaces, or sequences across different sections of the plan.
  - Verify that file paths, function signatures, and data shapes are consistent throughout.

- **Ambiguity and Divergent Interpretations**:
  - Flag ambiguous steps or interfaces where two different readers would build different things.
  - Require precise and unambiguous instructions for every component.
