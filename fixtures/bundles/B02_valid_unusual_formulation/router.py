"""Router written with unusual but equivalent constructs."""
THRESHOLD_SECONDS = 1800
MANUAL = "manual_review"


def route(ticket):
    if ticket.get("category") is None:  # rule: R1
        return MANUAL
    elif ticket["urgent"] == True:  # rule: R2
        return "priority"
    elif not (ticket.wait_minutes * 60 < THRESHOLD_SECONDS):  # rule: R3
        return "escalation"
    else:
        return "normal"
