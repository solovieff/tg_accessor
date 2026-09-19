"""Smart dialogue segmentation for exported Telegram group CSVs, powered by TypeSafe (Jev).

This package works offline on an already exported CSV (produced by the
Telegram provider, `providers.telegram`, or any future provider following
the same CSV contract)
and uses the TypeSafe System One API to make smarter thread/topic decisions than
plain reply-chain + time-window heuristics: it can link messages that continue the
same argument even when nobody pressed "Reply".
"""
