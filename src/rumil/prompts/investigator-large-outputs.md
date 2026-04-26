## Handling Large Outputs

Tool outputs sometimes exceed the Read tool's size limit and get saved to a file. When this happens:

- Use `Read` with `offset` and `limit` parameters to read the file in sections rather than attempting to read it all at once.
- Use `Grep` to search within the saved file for specific page IDs, headlines, or keywords rather than reading the entire file.
- Do not give up on large outputs — the information is still accessible, you just need to retrieve it in parts.
