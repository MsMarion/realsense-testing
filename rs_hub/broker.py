"""XSUB/XPUB forwarder: the hub every publisher and subscriber talks to.

Listens on localhost only by default. To let other machines (e.g. a Raspberry Pi
camera head) publish or subscribe, set RS_HUB_BIND=0.0.0.0 — this exposes every
camera feed to your network, so only do it on a network you trust.
"""
import os

import zmq

from rs_hub.bus import PUB_ADDR, SUB_ADDR


def main():
    host = os.environ.get("RS_HUB_BIND", "127.0.0.1")
    ctx = zmq.Context.instance()
    xsub = ctx.socket(zmq.XSUB)
    xsub.bind(PUB_ADDR.replace("127.0.0.1", host))
    xpub = ctx.socket(zmq.XPUB)
    xpub.bind(SUB_ADDR.replace("127.0.0.1", host))
    print(f"[broker] listening on {host}  publishers -> :5550   subscribers -> :5551", flush=True)
    try:
        zmq.proxy(xsub, xpub)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
