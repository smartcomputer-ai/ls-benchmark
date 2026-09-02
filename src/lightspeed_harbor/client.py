"""Lightspeed JSON-RPC client used from the Harbor host.

Planned responsibilities (see ``docs/next-steps.md``, slices 1 and 2): call
``initialize``, ``session/start``, ``session/environments/activate``,
``session/runs/start``, ``session/runs/read``, ``session/events/read``,
``session/runs/cancel``, ``session/close``, ``environments/close``, and
``environments/list`` against ``LIGHTSPEED_API_URL`` with
``Authorization: Bearer <LIGHTSPEED_API_KEY>``.

Parameter and result shapes come from the released contract in the sibling
checkout, ``crates/api/contract/api.schema.json``; the client must not depend
on Lightspeed source internals. Whether this module is hand-written over
``httpx`` or generated from ``openrpc.json`` is an open slice-1 decision.
"""
