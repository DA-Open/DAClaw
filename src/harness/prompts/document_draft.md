## Capability: document draft

Write a draft document from the material named in the task. This is a draft only: do not publish, send or share anything.

- Read every input file before writing. Use only facts that appear in the material; if something the task asks for is not in it, say so in the draft instead of inventing it.
- Write the draft as Markdown with `artifacts.write`, name `draft`, media type `text/markdown`.
- Use a Markdown heading (`#` or `##`) for each section the acceptance conditions list, with the section name as the heading text.
- When done, call `submit_result` with a one-line summary and `{"draft": "<artifact id>"}`.
