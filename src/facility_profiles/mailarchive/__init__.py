"""Archive of the customer group's booking mail in S3, on the doc-intake collector's pattern.

The lidl@circledelivers.com Google Group carries every pickup-appointment exchange for the pod,
but a Google Group has no mailbox of its own. The collector reads the group's traffic through a
member's mailbox (Gmail API, domain-wide delegation, read-only), keeps only the threads that are
about booking a pickup, and writes each message to S3 twice: the raw RFC822 (``.eml``, the form
that outlives mailboxes) and a parsed envelope (``.json``) with the headers, the reply's own
words, the quoted history and the identifiers the booking agent matches on. Every attachment is
stored once, addressed by its content.

Keys are derived from the RFC ``Message-ID`` header rather than Gmail's per-mailbox id, so the
same message collected from two members' mailboxes is one object, and a later backfill from a
long-standing member merges into the same archive.
"""
