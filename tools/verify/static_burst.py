"""Proof run: fetch every dashboard file from a demo server in parallel bursts, the way a browser does
(one new connection per file, because the server answers HTTP/1.0), and count the requests that fail.
Usage: static_burst.py <python> <code copy> <parallel> <rounds>"""
import collections, os, socket, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from demo_run import Demo, end_on_kill  # noqa: E402

end_on_kill()

PY, COPY, PAR, ROUNDS = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
web = os.path.join(COPY, "accountdeck", "web")
paths = ["/", "/icon.svg"] + ["/static/" + os.path.relpath(os.path.join(d, f), web)
                              for d, _, fs in os.walk(web) for f in fs if f not in ("index.html", "icon.svg")]
demo = Demo(PY, COPY, "typical")
host, port = "127.0.0.1", int(demo.origin.rsplit(":", 1)[1])
results = collections.Counter(); slow = []; lock = threading.Lock()

def fetch(path):
    start = time.monotonic()
    try:
        s = socket.create_connection((host, port), timeout=10)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nAccept: */*\r\n\r\n".encode())
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        s.close()
        head, _, body = data.partition(b"\r\n\r\n")
        status = head.split(b" ", 2)[1].decode() if head.startswith(b"HTTP/") else "no-status"
        length = [int(l.split(b":")[1]) for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")]
        key = status if (status != "200" or (length and length[0] == len(body))) else "200-short-body"
    except Exception as exc:
        key = type(exc).__name__
    took = time.monotonic() - start
    with lock:
        results[key] += 1
        if took > 1.0:
            slow.append((round(took, 2), path))

try:
    for _ in range(ROUNDS):
        queue = list(paths)
        while queue:
            batch, queue = queue[:PAR], queue[PAR:]
            threads = [threading.Thread(target=fetch, args=(p,)) for p in batch]
            [t.start() for t in threads]; [t.join() for t in threads]
    print(f"parallel={PAR} rounds={ROUNDS} requests={sum(results.values())} results={dict(results)} slower-than-1s={len(slow)} {sorted(slow)[-3:]}")
finally:
    demo.stop()
