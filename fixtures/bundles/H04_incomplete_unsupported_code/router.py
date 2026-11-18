"""Router that computes wait time itself."""
import datetime


def route(ticket):
    # rule: R1
    if ticket.category is None:
        return "manual_review"
    # rule: R2
    if ticket.urgent:
        return "priority"
    waited = (datetime.datetime.now() - ticket.created_at).total_seconds() / 60
    # rule: R3
    if waited >= 30:
        return "escalation"
    return "normal"
