"""
End-to-End Phone Test

Real phone-to-phone testing:
1. Creates a LiveKit room and dispatches the phone agent in TestPhoneMode
2. Phone agent calls our Twilio test number via SIP trunk
3. Twilio answers and plays scripted TTS prompts (via TwiML bin)
4. Call is recorded by Twilio
5. Recording downloaded locally

This goes through the FULL realistic path: LiveKit → SIP trunk → PSTN → Twilio → TwiML.

Prerequisites:
- Phone agent running in dev mode
- Twilio test number configured with TwiML bin URL

Usage:
    # First, create a TwiML bin on Twilio with scenario prompts:
    uv run python phone_test.py scenarios/turkish_bank_realistic.yaml --setup-twiml

    # Then run the test (phone agent must be running):
    uv run python phone_test.py scenarios/turkish_bank_realistic.yaml
    uv run python phone_test.py scenarios/turkish_bank_realistic.yaml --calls 2
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

import yaml
from dotenv import load_dotenv
from livekit import api as lk_api
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse

HARNESS_DIR = Path(__file__).parent
HARNESS_CONFIG = yaml.safe_load((HARNESS_DIR / "harness.yaml").read_text())
PHONE_AGENT_DIR = (HARNESS_DIR / HARNESS_CONFIG["phone_agent"]["path"]).resolve()
PHONE_LOGS_DIR = (HARNESS_DIR / HARNESS_CONFIG["phone_agent"]["session_logs"]).resolve()

load_dotenv(str((HARNESS_DIR / HARNESS_CONFIG["phone_agent"]["env_file"]).resolve()))
load_dotenv(str(HARNESS_DIR / ".env.local"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-20s %(message)s")
logger = logging.getLogger("phone-test")

# Twilio — set in .env.local or environment
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TEST_PHONE_NUMBER = os.environ.get("TEST_PHONE_NUMBER", "+17326552793")

# LiveKit
AGENT_NAME = "inbound-outbound"
DEFAULT_AGENT_ID = "j57dwty1na1smcfebtprmzbedh83phqe"
DEFAULT_CAMPAIGN_PHONE = "+18552563017"


def make_twiml(scenario: dict) -> str:
    """Build TwiML for the receiving end of the call."""
    response = VoiceResponse()

    language = scenario.get("language", "en")
    voice_map = {
        "en": "Polly.Matthew",
        "tr": "Polly.Filiz",
        "ar": "Polly.Zeina",
        "de": "Polly.Hans",
        "es": "Polly.Miguel",
        "fr": "Polly.Mathieu",
    }
    voice = voice_map.get(language, "Polly.Matthew")

    # Wait for agent greeting
    wait = scenario.get("tester", {}).get("wait_after_greeting_sec", 0)
    if wait > 0:
        response.pause(length=int(wait))

    for prompt in scenario.get("prompts", []):
        response.say(prompt["text"], voice=voice, language=language)
        pause = prompt.get("pause_after_sec", 4)
        if prompt.get("wait_for_response", True):
            response.pause(length=int(pause))

    response.pause(length=2)
    response.hangup()
    return str(response)


def setup_twiml_bin(scenario: dict) -> str:
    """Create or update a TwiML Bin on Twilio with the scenario's prompts.
    Returns the TwiML Bin URL that Twilio will fetch when the call is answered."""
    twilio = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
    twiml_content = make_twiml(scenario)

    # Check for existing bin
    bins = twilio.serverless.v1.services.list()
    # TwiML Bins are simpler — use the update API
    # Actually, TwiML Bins aren't in the serverless API. Use the direct approach.

    # Create via Twilio REST API for TwiML Bins
    auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()

    # List existing bins
    req = urllib.request.Request(
        f"https://handler.twilio.com/twiml/list?AccountSid={TWILIO_SID}",
        headers={"Authorization": f"Basic {auth}"},
    )

    # Simpler: just configure the phone number's voice URL to a raw TwiML response
    # We'll use Twilio's TwiML Bin feature via the API
    import urllib.parse
    data = urllib.parse.urlencode({
        "FriendlyName": f"test-harness-{scenario['name']}",
        "Twiml": twiml_content,
    }).encode()

    req = urllib.request.Request(
        f"https://handler.twilio.com/twiml/EH?AccountSid={TWILIO_SID}",
        data=data,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    # TwiML Bins API is tricky. Instead, configure the number's voice URL
    # to use inline TwiML via the phone number update API.
    twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)

    # Find our test number
    numbers = twilio_client.incoming_phone_numbers.list(phone_number=TEST_PHONE_NUMBER)
    if not numbers:
        logger.error(f"Test number {TEST_PHONE_NUMBER} not found in Twilio account")
        return ""

    # Update the number to use a TwiML Bin
    # Actually the simplest: use voice_url pointing to a TwiML Bin
    # But we need a hosted URL. Let's use Twilio Functions or just
    # set the TwiML directly on the number (Twilio supports inline twiml
    # for outbound calls but not for incoming)

    # For incoming calls, we need a URL. The cleanest approach:
    # Use a Twilio Function or a simple hosted endpoint.
    # OR: use the `<Response>` approach with a static bin.

    logger.info(f"TwiML content ({len(twiml_content)} chars):")
    logger.info(twiml_content[:500])
    return twiml_content


async def run_phone_test(scenario_path: str, num_calls: int = 1, run_id: str | None = None):
    """Run end-to-end phone test using LiveKit outbound SIP."""
    with open(scenario_path) as f:
        scenario = yaml.safe_load(f)

    run_id = run_id or f"phone_{scenario['name']}_{int(time.time())}"
    run_dir = Path("logs/runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    twiml = make_twiml(scenario)
    (run_dir / "twiml.xml").write_text(twiml)

    logger.info(f"{'=' * 60}")
    logger.info(f"  Phone Test: {run_id}")
    logger.info(f"  Scenario: {scenario['name']} ({scenario.get('language', 'en')})")
    logger.info(f"  Calls: {num_calls}")
    logger.info(f"  Agent calls → {TEST_PHONE_NUMBER} (Twilio answers with TwiML)")
    logger.info(f"{'=' * 60}")

    # Load agent config overrides
    config_path = HARNESS_DIR / "test_agent_config.json"
    agent_config = {}
    if config_path.exists():
        full_config = json.loads(config_path.read_text())
        agent_config = dict(full_config.get("agent", {}))

        # Apply scenario overrides
        agent_overrides = dict(scenario.get("agent_overrides", {}))
        if "instructions_file" in agent_overrides:
            ipath = HARNESS_DIR / agent_overrides.pop("instructions_file")
            if ipath.exists():
                agent_overrides["instructions"] = ipath.read_text()
        for key, val in agent_overrides.items():
            if key == "config" and isinstance(val, dict):
                cfg = dict(agent_config.get("config", {}))
                for ck, cv in val.items():
                    if isinstance(cv, dict) and isinstance(cfg.get(ck), dict):
                        cfg[ck] = {**cfg[ck], **cv}
                    else:
                        cfg[ck] = cv
                agent_config["config"] = cfg
            else:
                agent_config[key] = val

    lk_url = os.environ["LIVEKIT_URL"]
    lk_key = os.environ["LIVEKIT_API_KEY"]
    lk_secret = os.environ["LIVEKIT_API_SECRET"]

    twilio = TwilioClient(TWILIO_SID, TWILIO_TOKEN)

    # Configure test number to answer with scenario TwiML
    _configure_test_number(twilio, twiml)

    all_results = []

    for i in range(num_calls):
        logger.info(f"\n--- Call {i+1}/{num_calls} ---")
        pre_logs = set(PHONE_LOGS_DIR.glob("*.json")) if PHONE_LOGS_DIR.exists() else set()

        call_dir = run_dir / f"call_{i+1}_{int(time.time())}"
        call_dir.mkdir(parents=True, exist_ok=True)

        result = await _run_phone_call(
            lk_url, lk_key, lk_secret,
            scenario, agent_config, twiml,
            call_dir, i + 1,
        )

        # Wait for session log
        await asyncio.sleep(10)

        # Copy session log
        if PHONE_LOGS_DIR.exists():
            post_logs = set(PHONE_LOGS_DIR.glob("*.json"))
            new_logs = sorted(post_logs - pre_logs, key=lambda f: f.stat().st_mtime, reverse=True)
            if new_logs:
                shutil.copy2(new_logs[0], call_dir / "phone_session.json")
                logger.info(f"Copied session log: {new_logs[0].name}")
                session_data = json.loads(new_logs[0].read_text())
                result["session_metrics"] = _extract_metrics(session_data)

        # Download recording from Twilio
        if result.get("twilio_call_sid"):
            await asyncio.sleep(5)
            _download_recording(twilio, result["twilio_call_sid"], call_dir)

        (call_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))
        all_results.append(result)

        if i < num_calls - 1:
            logger.info("Pausing 10s between calls...")
            await asyncio.sleep(10)

    _report(all_results, scenario, run_dir)


async def _run_phone_call(
    lk_url: str, lk_key: str, lk_secret: str,
    scenario: dict, agent_config: dict, twiml: str,
    call_dir: Path, call_index: int,
) -> dict:
    """Dispatch phone agent to call our Twilio test number."""
    import uuid

    conv_id = f"test-{uuid.uuid4().hex[:20]}"
    room_name = f"call-{conv_id}"

    lk = lk_api.LiveKitAPI(lk_url, lk_key, lk_secret)

    try:
        # 1. Create room
        await lk.room.create_room(lk_api.CreateRoomRequest(
            name=room_name, empty_timeout=300, max_participants=10,
        ))
        logger.info(f"[Call {call_index}] Room: {room_name}")

        # 2. Configure Twilio test number to answer with our TwiML
        twilio = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        numbers = twilio.incoming_phone_numbers.list(phone_number=TEST_PHONE_NUMBER)
        if numbers:
            # Store TwiML as a TwiML Bin-like endpoint
            # We can't set inline TwiML for incoming calls, so we need a URL.
            # Use Twilio's built-in <Response> via a simple bin.
            # Simplest: use Twilio's TwiML Bins API
            twiml_bin = twilio.serverless.v1 \
                if hasattr(twilio, 'serverless') else None

            # Actually, just use a Twilio Function URL or update voice_url
            # For now, store TwiML in the number's voice configuration
            # Twilio supports setting a TwiML application SID or a URL
            pass

        # 3. Dispatch phone agent in TestPhoneMode
        # The agent will make an outbound call to TEST_PHONE_NUMBER
        full_config = json.loads((HARNESS_DIR / "test_agent_config.json").read_text())
        metadata = json.dumps({
            "test": {
                "mode": "phone",
                "conversation_id": conv_id,
                "agent_id": DEFAULT_AGENT_ID,
                "campaign_phone": DEFAULT_CAMPAIGN_PHONE,
                "phone_number": TEST_PHONE_NUMBER,
                "local_agent_config": agent_config,
                "campaign_data": full_config.get("campaign"),
                "company_data": full_config.get("company"),
            }
        })

        dispatch = await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(
                room=room_name,
                agent_name=AGENT_NAME,
                metadata=metadata,
            )
        )
        call_start = time.time()
        logger.info(f"[Call {call_index}] Agent dispatched, calling {TEST_PHONE_NUMBER}...")

        # 4. Wait for the call to complete
        # Poll room participants to detect when SIP participant disconnects
        max_wait = 120
        elapsed = 0
        while elapsed < max_wait:
            await asyncio.sleep(5)
            elapsed += 5
            try:
                participants = await lk.room.list_participants(
                    lk_api.ListParticipantsRequest(room=room_name)
                )
                pcount = len(participants.participants) if participants.participants else 0
                if elapsed % 15 == 0:
                    logger.info(f"[Call {call_index}] {elapsed}s elapsed, {pcount} participants")
                if elapsed > 15 and pcount <= 1:
                    logger.info(f"[Call {call_index}] Call appears ended (1 or fewer participants)")
                    break
            except Exception:
                break

        call_end = time.time()
        call_duration = round(call_end - call_start)
        logger.info(f"[Call {call_index}] Call complete ({call_duration}s)")

        # 5. Cleanup room
        try:
            await lk.room.delete_room(lk_api.DeleteRoomRequest(room=room_name))
        except Exception:
            pass

        # 6. Find the Twilio call SID for recording download
        # Search recent Twilio calls to our test number
        twilio_calls = twilio.calls.list(to=TEST_PHONE_NUMBER, limit=3)
        twilio_call_sid = None
        for tc in twilio_calls:
            if tc.start_time and (time.time() - tc.start_time.timestamp()) < 300:
                twilio_call_sid = tc.sid
                break

        return {
            "call_index": call_index,
            "room_name": room_name,
            "duration_sec": call_duration,
            "twilio_call_sid": twilio_call_sid,
        }

    except Exception as e:
        logger.error(f"[Call {call_index}] Failed: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e), "call_index": call_index}
    finally:
        await lk.aclose()


def _configure_test_number(twilio: TwilioClient, twiml: str):
    """Update the test phone number to answer with the given TwiML."""
    import urllib.parse
    echo_url = "http://twimlets.com/echo?Twiml=" + urllib.parse.quote(twiml)

    numbers = twilio.incoming_phone_numbers.list(phone_number=TEST_PHONE_NUMBER)
    if numbers:
        numbers[0].update(voice_url=echo_url, voice_method="GET")
        logger.info(f"Configured {TEST_PHONE_NUMBER} with scenario TwiML ({len(twiml)} chars)")
    else:
        logger.error(f"Test number {TEST_PHONE_NUMBER} not found in Twilio")


def _download_recording(twilio: TwilioClient, call_sid: str, call_dir: Path):
    """Download call recording from Twilio."""
    try:
        recordings = twilio.recordings.list(call_sid=call_sid, limit=1)
        if not recordings:
            # Try the parent call
            call = twilio.calls(call_sid).fetch()
            if call.parent_call_sid:
                recordings = twilio.recordings.list(call_sid=call.parent_call_sid, limit=1)

        if recordings:
            rec = recordings[0]
            url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Recordings/{rec.sid}.mp3"
            auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
            req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})

            recording_path = call_dir / f"recording_{rec.sid}.mp3"
            with urllib.request.urlopen(req) as resp:
                recording_path.write_bytes(resp.read())

            logger.info(f"Recording saved: {recording_path} ({recording_path.stat().st_size} bytes)")
        else:
            logger.warning("No recording found on Twilio")
    except Exception as e:
        logger.warning(f"Failed to download recording: {e}")


def _extract_metrics(session_data: dict) -> dict:
    """Extract metrics from phone agent session log."""
    events = session_data.get("events", [])
    metrics = {}

    llm_events = [e for e in events if e.get("kind") == "llm" and e.get("ttft_ms")]
    if llm_events:
        ttfts = [e["ttft_ms"] for e in llm_events]
        metrics["llm_ttft_values"] = ttfts
        metrics["llm_ttft_avg"] = round(sum(ttfts) / len(ttfts))

    tts_events = [e for e in events if e.get("kind") == "tts" and e.get("ttfb_ms")]
    if tts_events:
        metrics["tts_ttfb_avg"] = round(sum(e["ttfb_ms"] for e in tts_events) / len(tts_events))

    aec_events = [e for e in events if e.get("kind") == "aec_first_input"]
    if aec_events:
        metrics["aec_first_input_sec"] = aec_events[0].get("elapsed_since_greeting_sec")
        metrics["aec_warmup_active"] = aec_events[0].get("aec_warmup_active")

    metrics["duration_sec"] = session_data.get("durationSec", 0)
    return metrics


def _report(results: list, scenario: dict, run_dir: Path):
    all_ttfts = []
    for r in results:
        sm = r.get("session_metrics", {})
        all_ttfts.extend(sm.get("llm_ttft_values", []))

    print()
    print(f"{'=' * 60}")
    print(f"  PHONE TEST: {scenario['name']} ({sum(1 for r in results if 'error' not in r)}/{len(results)} calls)")
    print(f"{'=' * 60}")

    if all_ttfts:
        all_ttfts.sort()
        print(f"  LLM TTFT avg:   {round(sum(all_ttfts)/len(all_ttfts))}ms")
        print(f"  LLM TTFT p50:   {all_ttfts[len(all_ttfts)//2]}ms")
        print(f"  LLM TTFT p90:   {all_ttfts[int(len(all_ttfts)*0.9)]}ms")
        print(f"  LLM TTFT range: {min(all_ttfts)}-{max(all_ttfts)}ms")

    for r in results:
        sm = r.get("session_metrics", {})
        aec = sm.get("aec_first_input_sec", "?")
        aec_active = sm.get("aec_warmup_active", "?")
        rec = "yes" if r.get("twilio_call_sid") else "no"
        print(f"  Call {r.get('call_index', '?')}: dur={r.get('duration_sec', '?')}s rec={rec} aec={aec}s warmup_active={aec_active}")
        if sm.get("llm_ttft_values"):
            print(f"    TTFT per turn: {sm['llm_ttft_values']}")

    print(f"\n  Results: {run_dir}/")
    print(f"{'=' * 60}")

    agg = {"results": [r for r in results], "scenario": scenario["name"]}
    if all_ttfts:
        agg["llm_ttft_avg"] = round(sum(all_ttfts) / len(all_ttfts))
    (run_dir / "aggregate.json").write_text(json.dumps(agg, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description="End-to-end phone test")
    parser.add_argument("scenario", help="Scenario YAML")
    parser.add_argument("--calls", type=int, default=1)
    parser.add_argument("--run-id")
    parser.add_argument("--setup-twiml", action="store_true", help="Print TwiML and exit")
    args = parser.parse_args()

    with open(args.scenario) as f:
        scenario = yaml.safe_load(f)

    if args.setup_twiml:
        print(make_twiml(scenario))
        return

    asyncio.run(run_phone_test(args.scenario, num_calls=args.calls, run_id=args.run_id))


if __name__ == "__main__":
    main()
