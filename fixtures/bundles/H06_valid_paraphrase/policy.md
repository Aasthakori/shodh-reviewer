<!-- artifact: routing-policy, version: 1.0 -->
# Ticket routing policy

Rules are applied top to bottom.

- R1: If category is missing then route to manual review.
- R2: If urgent is true then route to priority queue.
- R3: If waiting time is at least 30 minutes then route to escalation queue.
- DEFAULT: Otherwise route to normal queue.
