# ADR-001: Stack versions

Context: the repository held a placeholder skeleton (Django 6.1, Python 3.14, `core/` project package).
Decision: follow the plan: Django 5.2 LTS, Python >=3.12 (3.13 in this environment), project package `config/`, apps under `apps/`. The skeleton was removed.
Consequences: existing placeholder endpoints (`/api/health`) are replaced by `/healthz` and `/readyz`.
