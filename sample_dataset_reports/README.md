# Data Validation Test Data

Each primary source dataset contains 600 rows. Case 3 also contains target-only and source-only rows.

## Expected scenarios
- **Case 1:** Genuine mismatches plus an extra column. Also contains fixable whitespace, NULL/empty, and `.0` formatting differences.
- **Case 2:** Logical match. Raw values intentionally contain formatting differences that should become a MATCH when your data-fix parameter is enabled.
- **Case 3:** Genuine mismatches, unmatched rows on both sides, an extra source column, and some fixable formatting differences.

The `customer_id` values are zero-padded identifiers such as `CUST00001`; they should remain unchanged by normalization.
