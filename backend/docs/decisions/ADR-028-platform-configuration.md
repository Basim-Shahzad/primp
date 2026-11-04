# ADR-028: Platform configuration

Status: accepted

## Context
Operational values (hold duration, reminder offsets, OTP limits, points expiry, ticket
priorities, ...) were Django settings, so changing one meant a deployment.

## Decision
- A typed registry (`backoffice/config_registry.py`) declares every tunable key with a strict
  Pydantic schema and a code default taken from Django settings.
- Overrides live in `PlatformSetting` (one row per key, with a `version`) and every change is
  appended to `PlatformSettingHistory`. Changes need `config.manage`, a recent re-authentication,
  the current `version` (409 `stale_version` otherwise) and write one audit row.
- Reads go through `config_service.get`, which uses a Redis cache (5 minutes) that is dropped on
  change (and again after commit). If the database or cache is down, the code default is used.
- Invalid stored values are ignored in favour of the default and logged.
- Notification templates and point rules are edited through their own endpoints, also
  versioned and audited. Template placeholders are limited to those the code supplies for that
  key (taken from the shipped defaults); broken braces and unknown names are rejected, and a
  preview renders sample values without saving.

## Consequences
Values change without a restart, bad values cannot be saved, and every change is attributable.
New tunables are added to the registry; code reads them with `config_service.get`.
