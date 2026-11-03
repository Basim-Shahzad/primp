# ADR-002: geo models are created in Step 5

Context: profile management (Step 5) needs default city/area, but the plan builds geo models in Step 6.
Decision: `geo` (City, Area) is created with the profile step; Step 6 adds catalog and businesses on top.
Consequences: accounts depends on geo selectors only (never its models directly in services beyond FKs).
