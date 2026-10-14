"""Bridge Cloud: accounts, plans and AI credit for the Bridge Mac app.

The Mac app sends its AI requests here instead of to OpenAI. This server checks the
account's credit, forwards the request with the server's own OpenAI key, and charges what
OpenAI says it cost. Payments are Lemon Squeezy's (it is the merchant of record and handles
sales tax); this server only listens to its webhooks. Request and answer contents are passed
through and never stored or logged.
"""
