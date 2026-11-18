"""Ticket router (linked from routing-spec)."""


def route(ticket):
    # rule: R1
    if ticket.category is None:
        return "manual_review"
    # rule: R2
    if ticket.urgent:
        return "priority"
    # rule: R3
    if ticket.wait_minutes >= 0.5:  # half an hour
        return "escalation"
    return "normal"
