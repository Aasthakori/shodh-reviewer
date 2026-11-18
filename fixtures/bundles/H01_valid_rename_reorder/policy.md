<!-- artifact: routing-policy, version: 1.0 -->
# Ticket routing policy

The order of the rules below defines precedence.

- POL-A: Missing category -> manual review.
- POL-B: Otherwise, urgent ticket -> priority queue.
- POL-C: Otherwise, waiting >= 30 minutes -> escalation.
- FALLBACK: All remaining tickets -> normal queue.
