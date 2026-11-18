<!-- artifact: routing-policy, version: 1.0 -->
# Ticket routing policy (alternate wording)

- R1: If category is missing then route to manual review.
- R2: If urgent is true then route to priority queue.
- R3: If wait is 30 minutes or more then route to escalation queue.
- DEFAULT: Otherwise route to normal queue.
