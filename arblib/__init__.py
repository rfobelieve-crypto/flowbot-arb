"""Arbitrage line: judgement, cost model, verdict scorers.

The engine that records and trades lives in ../engine (its own
process, its own credentials, its own account). Nothing in here
imports it -- this package only reads what the engine wrote.
"""
