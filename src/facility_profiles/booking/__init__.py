"""Booking agent prototype: email pickup appointments for customer-tendered inbound loads.

Draft mode only in this prototype. The agent finds loads whose pickup stop still needs an
appointment, composes the request email from the vendor's profile, reads the vendor's reply
through a structured classifier, and proposes the appointment for a person to approve. Nothing
is sent and nothing is written to Transport Pro without a human decision.
"""
