---
name: my-summary
description: Summarize a workspace area using local evidence when the user asks for an overview of its purpose, important files, or visible risks.
---

# Workspace Summary

Interpret the raw arguments as the workspace-relative directory to summarize. Use `.` when the user
does not provide a directory.

Inspect the requested area and summarize its purpose, important files, and visible risks. Prefer the
available read-only workspace and Git tools. Base every workspace claim on local evidence and include
evidence markers in the corresponding answer text.

Return concise prose under these headings when a structured answer is useful:

- Summary
- Important files
- Visible risks

Do not propose edits, commands, Web requests, or MCP calls unless the user explicitly asks for them.
