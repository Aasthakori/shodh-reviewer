<!-- artifact: routing-policy, version: 1.0 -->
# Ticket routing policy

The order of the rules below defines precedence.

- R1: Missing category -> manual review.
- R2: Otherwise, urgent ticket -> priority queue.
- R3: Otherwise, waiting >= 30 minutes -> escalation.
- DEFAULT: All remaining tickets -> normal queue.
