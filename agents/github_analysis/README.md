# GitHub Source Analysis Agent

Reads bounded files from allowlisted repositories and produces pinned source evidence for black-box test design.

**Contract:** `source.inspect` · `POST /v1/analyze` · port `8010`.

```mermaid
flowchart LR
  Ref[Repository + immutable ref] --> Analysis[GitHub Source Analysis]
  Analysis --> GitHub[GitHub contents API]
  GitHub --> Evidence[Blob/tree SHAs + candidate findings]
  Evidence --> Generation[Test Generation]
```

Access is read-only and bounded by repository allowlists, file count, and byte limits. Candidate findings are not confirmed defects.
