<!-- artifact: routing-policy, version: 1.0 -->
# Ticket routing policy

- R1: Missing category -> manual review.
- R2: Otherwise, urgent ticket -> priority queue.
- R3: If the customer seems upset then route to escalation queue.
- R4: Otherwise, waiting >= 30 minutes -> escalation.
- DEFAULT: All remaining tickets -> normal queue.
