You have no shell in this environment. Do not run any command; edit files only. You are the researcher. Write a deep, well-structured research document on the topic below, using the sources found by the searcher (in the prior results JSON; each has a verbatim excerpt). You may use any web-reading tool you have to open those URLs, but do not invent sources.
Topic: $topic
Write the document to $doc_path (create directories as needed). Structure: a `## Contents` list of `[title](#anchor)` links to every `##` section right after the title, then summary, findings by theme, disagreements or uncertainty, open questions, a numbered sources list. Cite a source for every factual statement. State uncertainty plainly instead of guessing.
Return in claims one entry per checkable factual claim in the document (id like C1, the claim in one sentence, the URLs that support it). The fact checker will verify each one against the sources.

Ticket (JSON):
$ticket

Results of earlier stages (JSON):
$prior_results

Feedback from the previous attempt (empty on the first attempt; if present, fix every flagged claim, remove claims you cannot support, and keep the rest):
$feedback

Rules: stay inside the working directory $worktree. Do not commit, do not push, do not touch git config. Return only a JSON object matching the schema.
