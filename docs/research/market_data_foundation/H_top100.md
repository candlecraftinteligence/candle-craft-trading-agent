# H. Global Top-100 shadow

The boundary is ranks 1 through 100 before exclusions. Rank 101 is never used to refill a stable, wrapped, ambiguous, or unsupported asset.

Identity is a CMC-id crosswalk with source, retrieval time, and record id. A unique ticker is only an unverified candidate. It does not increment identity-validated or eligible counts. Quote, settlement, contract type, and multiplier must match the crosswalk. Prefixes are not stripped. Wrapped assets are not merged.

The earlier live summary's 100 validated and 74 eligible figures were ticker joins without that crosswalk. Those counts are withdrawn. They are not restated as validated identity.

An incomplete or temporally invalid ranking does not feed the sample queue. Structural rank completeness and timestamp validity are separate statuses.

Owner monitoring is a separate count. This phase does not execute the production scanner, so scan attempts are zero.

Live Top-100 summary: not attached.
