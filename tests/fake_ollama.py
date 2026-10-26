"""A stand-in for the Ollama HTTP API, for tests and for trying the AI tab without a model.

    python tests/fake_ollama.py [port]      # default 11434

Implements /api/version, /api/tags and /api/chat (streaming and not, with a JSON
schema in "format"). Report answers are built from the months named in the prompt.
"""
import csv
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = [{"name": "qwen3:8b", "details": {"parameter_size": "8.2B"}},
          {"name": "llama3.2:3b", "details": {"parameter_size": "3.2B"}}]


class State:
    requests: list[dict] = []
    delay = 0.0          # seconds before answering a report, to show the "working" state
    fail_with = ""       # an error message Ollama should return
    think = False        # stream some "thinking" before the answer


def expense_report(months: list[str]) -> dict:
    out = []
    for i, m in enumerate(months):
        cats = [{"category": "Food", "amount": 610 + 10 * i}, {"category": "Housing", "amount": 2150},
                {"category": "Transportation", "amount": 320 + (90 if m.endswith("-12") else 0)}]
        out.append({"month": m, "categories": cats, "total": sum(c["amount"] for c in cats)})
    if out:
        out[-1]["total"] += 100  # small models get sums wrong; the app must notice
    return {"summary": "Spending stays close to recent months, with a seasonal bump in December.",
            "months": out,
            "observations": ["Food has crept up about 4% over the last 6 months.",
                             "Housing is stable: rent is the largest fixed cost."],
            "recommendations": ["Set a December budget for gifts and travel.",
                                "Review subscriptions under Entertainment."]}


def income_report(months: list[str], text: str) -> dict:
    assets = []
    for line in text.split("## Assets and debts", 1)[-1].splitlines()[2:]:
        if not line.strip() or line.startswith("##"):
            break
        parts = next(csv.reader([line]))
        if len(parts) >= 6 and parts[3] not in ("", "unknown"):
            now, rate = float(parts[3]), float(parts[5])
            assets.append({"name": parts[0], "now": now, "in_1_year": round(now * (1 + rate / 100), 2),
                           "in_5_years": round(now * (1 + rate / 100) ** 5, 2)})
    total = sum(a["now"] for a in assets)
    return {"summary": "Income is steady from regular pay cheques.",
            "income_months": [{"month": m, "amount": 6200} for m in months],
            "assets": assets,
            "net_worth": {"now": total, "in_1_year": round(total * 1.03, 2), "in_5_years": round(total * 1.16, 2)},
            "observations": ["Two pay cheques arrive in most months, three in two months."],
            "recommendations": ["Move the surplus to the TFSA each month."]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/version":
            return self._json({"version": "0.12.6-fake"})
        if self.path == "/api/tags":
            return self._json({"models": MODELS})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        State.requests.append(req)
        if self.path != "/api/chat":
            return self._json({"error": "not found"}, 404)
        if State.fail_with:
            return self._json({"error": State.fail_with}, 500)
        if req["model"] not in [m["name"] for m in MODELS]:
            return self._json({"error": f"model '{req['model']}' not found"}, 404)
        prompt = req["messages"][-1]["content"]
        if req.get("format"):
            time.sleep(State.delay)
            months = re.findall(r"\b(\d{4}-\d{2})\b", prompt.split("## Task", 1)[-1])
            props = req["format"].get("properties", {})
            report = expense_report(months) if "months" in props else income_report(months, prompt)
            return self._json({"model": req["model"], "message": {"role": "assistant", "content": json.dumps(report)},
                               "done": True})
        answer = ("Over the last 3 months you spent the most on **Housing**, then Food.\n\n"
                  "| Category | Total |\n|---|---|\n| Housing | $6,450 |\n| Food | $1,830 |\n\n"
                  "- Rent is fixed\n- Food went up in September")
        if not req.get("stream", True):
            return self._json({"message": {"role": "assistant", "content": answer}, "done": True})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(obj):
            data = (json.dumps(obj) + "\n").encode()
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        if State.think:
            send({"message": {"role": "assistant", "content": "", "thinking": "Adding up the months…"}, "done": False})
        for word in re.findall(r"\S+\s*", answer):
            send({"message": {"role": "assistant", "content": word}, "done": False})
            time.sleep(State.delay / 50)
        send({"message": {"role": "assistant", "content": ""}, "done": True})
        self.wfile.write(b"0\r\n\r\n")

    def log_message(self, *args):
        pass


def start(port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    State.delay = 3.0  # make the "working" state visible when trying it out
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11434
    print(f"Fake Ollama on http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
