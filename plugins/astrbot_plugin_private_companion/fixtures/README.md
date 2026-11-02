# Fixtures

G0 fixtures must be synthetic and non-sensitive. No production database, message body,
device credential, image original, or host configuration belongs in this directory.

`astrbot_staging.example.json` is a synthetic preview configuration for the
trusted staging bootstrap. Its account IDs, persona/provider fingerprints and
budgets come from local test doubles. They are not live host settings. Validate
its format with `tools/validate_staging_config.py`. Runtime authority and
lifecycle checks live in the host adapter and its tests.
