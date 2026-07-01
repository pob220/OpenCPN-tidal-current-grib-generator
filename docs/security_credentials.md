# Security and credentials

## Rules

- Do not hard-code credentials.
- Do not commit credentials.
- Do not log credentials.
- Do not pass passwords as command-line arguments.
- Do not store passwords in v1.

The plugin should prompt for Copernicus username/password at runtime. It may remember the username only.

If password storage is considered later, use OS keychain/keyring integration. Do not implement plain-text password storage.

## Live tests

Live Copernicus tests are opt-in and skipped unless these environment variables are set:

```bash
CURRENTGRIB_TEST_COPERNICUS_USERNAME
CURRENTGRIB_TEST_COPERNICUS_PASSWORD
```

The test download must be a very small area/time range. Test output directories are ignored by git.

## Implementation notes

The Python helper uses the Copernicus Marine Toolbox Python API so credentials are passed in-process rather than exposed in process lists as command-line arguments. Progress details use redacted summaries.

The OpenCPN plugin invokes the helper command for v1. For Copernicus generation it sets
`CURRENTGRIB_COPERNICUS_PASSWORD` only for the duration of the worker subprocess and restores
or unsets it immediately after the command exits. The password is not added to argv, log text,
temporary files, or plugin settings. This is still a transient process environment handoff, not
persistent password storage.
