# Disposable database approval for reporting canaries

Approved by the user on 10 September 2026 for disposable local and CI test databases. The approval includes the seven migrations below and does not authorise additional migrations or production schema changes.

The reporting changes add no migrations. The current backend main branch includes seven additional Roo migrations since the previously approved reporting test run. This approval concerns constructing a disposable local test database and disposable CI test databases from current main, including the following exact migrations. It does not authorize new production schema changes.

Required by [AGENTS.md](../AGENTS.md): “Never create, run, or apply a database migration without explicit user approval for that specific migration.”

- [`roo.0034_officemanagerday_coworkingbooking_booking_source_and_more`](../roo/migrations/0034_officemanagerday_coworkingbooking_booking_source_and_more.py)
- [`roo.0035_protect_office_manager_assignment_day`](../roo/migrations/0035_protect_office_manager_assignment_day.py)
- [`roo.0036_office_manager_attempts_and_provenance`](../roo/migrations/0036_office_manager_attempts_and_provenance.py)
- [`roo.0037_quarantine_legacy_office_manager_provenance`](../roo/migrations/0037_quarantine_legacy_office_manager_provenance.py)
- [`roo.0038_office_manager_claim_generation`](../roo/migrations/0038_office_manager_claim_generation.py)
- [`roo.0039_supersede_reopened_office_manager_attempts`](../roo/migrations/0039_supersede_reopened_office_manager_attempts.py)
- [`roo.0040_merge_coworking_operations_office_manager`](../roo/migrations/0040_merge_coworking_operations_office_manager.py)
