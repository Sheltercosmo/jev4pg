"""Deterministic deployment fixture. These responses do not measure model quality."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.headers.get("Authorization"):
            self.send_error(400, "Unexpected credential on the public test endpoint")
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        answers = {}
        for key, question in request["questions"].items():
            if question["type"] != "noul":
                self.send_error(400, "This deployment fixture only implements Noul")
                return
            instructions = question["instructions"]
            definition = (
                instructions.get("definition") if isinstance(instructions, dict) else instructions
            )
            probability = {"false": 0.05, "unknown": 0.5}.get(definition, 0.95)
            answers[key] = {"type": "noul", "noul": probability}
        response = json.dumps(
            {
                "model": request["model"],
                "answers": answers,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9100), Handler).serve_forever()
