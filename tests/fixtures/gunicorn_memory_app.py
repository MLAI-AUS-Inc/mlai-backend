"""Disposable CI-only allocation; no Django, account, provider or database."""


def application(environ, start_response):
    """Touch a small temporary buffer and return only a fixed acknowledgement."""
    scratch = bytearray(4 * 1024 * 1024)
    scratch[::4096] = b"x" * len(scratch[::4096])
    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
    return [b"ok"]
