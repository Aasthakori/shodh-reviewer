"""Ticket router (linked from routing-spec)."""


def route(ticket):
    # rule: POL-A
    if ticket.category is None:
        return "manual_review"
    # rule: POL-B
    if ticket.urgent:
        return "priority"
    # rule: POL-C
    if ticket.wait_minutes >= 30:
        return "escalation"
    return "normal"
