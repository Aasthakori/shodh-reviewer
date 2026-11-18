"""Ticket router (linked from routing-spec)."""


def route(ticket):
    # rule: R1
    if ticket.category is None:
        return "manual_review"
    # rule: R2
    if ticket.urgent:
        return "priority"
    # rule: R4
    if ticket.wait_minutes >= 30:
        return "escalation"
    return "normal"
