"""XSUB/XPUB forwarder: the hub every publisher and subscriber talks to.

Binds to RS_HUB_BIND: 0.0.0.0 (every network interface, so other machines can publish
and subscribe) when started by launch.py, 127.0.0.1 (this machine only) when run on its
own without the variable. There is no authentication: anyone who can reach ports
5550/5551 can read every camera feed and publish onto the bus.
"""
import os

import zmq

from rs_hub.bus import PUB_PORT, SUB_PORT


def main():
    host = os.environ.get("RS_HUB_BIND", "127.0.0.1")
    ctx = zmq.Context.instance()
    xsub = ctx.socket(zmq.XSUB)
    xsub.bind(f"tcp://{host}:{PUB_PORT}")
    xpub = ctx.socket(zmq.XPUB)
    xpub.bind(f"tcp://{host}:{SUB_PORT}")
    print(f"[broker] listening on {host}  publishers -> :{PUB_PORT}   subscribers -> :{SUB_PORT}", flush=True)
    try:
        zmq.proxy(xsub, xpub)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
