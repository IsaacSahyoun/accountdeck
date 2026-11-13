"""Proof run: fetch every dashboard file from a demo server in parallel bursts, the way a browser does
(one new connection per file, because the server answers HTTP/1.0), and count the requests that fail.
Usage: static_burst.py <python> <code copy> <parallel> <rounds>"""
import collections, glob, http.cookiejar, json, os, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request

PY, COPY, PAR, ROUNDS = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
web = os.path.join(COPY, "accountdeck", "web")
paths = ["/", "/icon.svg"] + ["/static/" + os.path.relpath(os.path.join(d, f), web)
                              for d, _, fs in os.walk(web) for f in fs if f not in ("index.html", "icon.svg")]
tmp = tempfile.gettempdir()
before = set(glob.glob(os.path.join(tmp, "accountdeck2-demo-*")))
proc = subprocess.Popen([PY, "-E", "-s", "run.py", "--demo", "typical", "--port", "0", "--no-browser"], cwd=COPY,
                        env=dict(os.environ, ACCOUNTDECK_SAFE_MODE="1"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
origin, token = proc.stdout.readline().strip().split(": ", 1)[1].split("/#token=")
host, port = "127.0.0.1", int(origin.rsplit(":", 1)[1])
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
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def post(path, body):
        req = urllib.request.Request(origin + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-AccountDeck": "1", "Origin": origin})
        try: return opener.open(req, timeout=15).status
        except urllib.error.HTTPError as e: return e.code
    try:
        post("/api/session", {"token": token}); post("/api/stop", {}); proc.wait(timeout=15)
    except Exception:
        proc.kill(); proc.wait()
    for d in set(glob.glob(os.path.join(tmp, "accountdeck2-demo-*"))) - before:
        shutil.rmtree(d, ignore_errors=True)
