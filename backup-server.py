#!/usr/bin/env python3
"""Maly serwer HTTP wyzwalajacy backup. Slucha tylko na localhoscie -
kontener dociera do niego przez host.docker.internal."""
import http.server
import subprocess
import threading
import time

SCRIPT = "/Users/zerx/stacks/riot/backup.sh"
MIN_INTERVAL = 300          # nie czesciej niz raz na 5 minut
_last = [0.0]
_lock = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/backup":
            self.send_error(404)
            return
        with _lock:
            now = time.time()
            if now - _last[0] < MIN_INTERVAL:
                self.respond(429, "za wczesnie, backup byl niedawno")
                return
            _last[0] = now
        threading.Thread(target=self.run_backup, daemon=True).start()
        self.respond(202, "backup wystartowal")

    def run_backup(self):
        try:
            r = subprocess.run([SCRIPT], capture_output=True, text=True, timeout=600)
            print(r.stdout.strip() or r.stderr.strip(), flush=True)
        except Exception as e:
            print(f"blad backupu: {e}", flush=True)

    def respond(self, code, msg):
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("backup-server slucha na 127.0.0.1:8181", flush=True)
    http.server.HTTPServer(("127.0.0.1", 8181), Handler).serve_forever()
