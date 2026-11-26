# Artifact and report contract

policy.json is an installed package resource, not a working-directory
file. Both wheel and sdist must carry all runtime resources. A wheel
rebuilt from the sdist has the same behavior as the directly built wheel.
The pyproject tool.bench.package include patterns control these manifests.
Run from any working directory, including without the source checkout.

Render HTML with text and attribute quote escaping. Manifest SHA-256
processes filenames in Unicode codepoint order: UTF-8 name, NUL, UTF-8
content, NUL for each entry. ZIP output sorts members, uses stored files,
mode 0644, and the fixed timestamp 2024-01-01 00:00:00 regardless of built_at.
CSV uses RFC 4180 quoting and CRLF record separators, retaining embedded
newlines. Durations accept nonnegative ISO PT hours/minutes/decimal seconds
and report integer milliseconds. Empty PT and day notation are rejected.

Group names after NFC normalization (preserve case). Weighted means use
counts, not an average of group means; emit three decimal places or null
when total count is zero. Sort semantic versions numerically, prereleases
before releases, numeric prerelease identifiers before strings; ignore
+build metadata for ordering. SHA-256 of a base64 artifact hashes decoded
bytes verbatim, including non-UTF-8 bytes.

TAR inspection accepts regular files only. Reject absolute paths, drive
paths and parent components on both POSIX and Windows; normalize slash
direction and leading ./ before duplicate detection. Reject duplicate
canonical paths rather than selecting a first/last winner. These rules
apply even when inspection runs on a different host OS from extraction.
