"""
Minimal TwiML server — serves scenario prompts when Twilio calls our test number.

Starts an HTTP server on a local port, exposed via ngrok or similar,
that returns TwiML telling Twilio what to say into the call.

Usage:
    uv run python twiml_server.py scenarios/turkish_bank_realistic.yaml --port 8765
"""

import argparse
import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import yaml
from twilio.twiml.voice_response import VoiceResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-12s %(message)s")
logger = logging.getLogger("twiml-server")

_scenario = None


class TwiMLHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        return self._serve_twiml()

    def do_POST(self):
        return self._serve_twiml()

    def _serve_twiml(self):
        response = VoiceResponse()

        if _scenario is None:
            response.say("No scenario loaded.")
            self._respond(str(response))
            return

        language = _scenario.get("language", "en")
        voice_map = {
            "en": "Polly.Matthew",
            "tr": "Polly.Filiz",
            "ar": "Polly.Zeina",
            "de": "Polly.Hans",
            "es": "Polly.Miguel",
            "fr": "Polly.Mathieu",
        }
        voice = voice_map.get(language, "Polly.Matthew")

        # Initial pause to hear greeting
        wait = _scenario.get("tester", {}).get("wait_after_greeting_sec", 0)
        if wait > 0:
            response.pause(length=int(wait))

        for prompt in _scenario.get("prompts", []):
            response.say(prompt["text"], voice=voice, language=language)
            pause = prompt.get("pause_after_sec", 4)
            if prompt.get("wait_for_response", True):
                response.pause(length=int(pause))

        response.pause(length=2)
        response.hangup()

        twiml = str(response)
        logger.info(f"Served TwiML ({len(twiml)} chars) for {_scenario['name']}")
        self._respond(twiml)

    def _respond(self, body: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        logger.debug(format % args)


def main():
    global _scenario
    parser = argparse.ArgumentParser(description="TwiML server for phone tests")
    parser.add_argument("scenario", help="Scenario YAML to serve")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    with open(args.scenario) as f:
        _scenario = yaml.safe_load(f)

    logger.info(f"Serving scenario: {_scenario['name']} on port {args.port}")
    logger.info(f"Configure Twilio webhook to: http://localhost:{args.port}/")

    server = HTTPServer(("0.0.0.0", args.port), TwiMLHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
