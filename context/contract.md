# Current ingestion contract

The input is CSV (RFC 4180, including quoted multiline cells) or JSONL.
CSV defaults accept UTF-8 with or without its signature. Decode strictly;
corrupt bytes must not be silently discarded. JSON numeric tokens must
retain decimal precision. Blank JSONL lines are ignored.

Merge defaults, selected profile, environment and explicit options in that
order, recursively. The Europe profile changes separators, not encoding.
minimum=null means use configured minimum; minimum=0 is an explicit zero.
Apply the configured thousands separator before the decimal separator.
Round each amount to cents using HALF_EVEN, including negative values.
Timestamp offsets are mandatory; convert instants to UTC (do not relabel
their wall clocks). The after watermark is exclusive.

schema_file chooses an alias map, including partner.json. Source paths in
a job configuration are relative to that configuration's directory, never
the process working directory. Compressed feeds can be extensionless;
recognize gzip by its magic bytes. Deduplication keeps the highest integer
revision per identifier, then sorts identifiers. Equal revisions keep the
first row. With deduplicate=false preserve input order.
