"""
End-to-End Phone Test

Real phone-to-phone testing with two LiveKit agents:
1. Phone agent (production) — dispatched as TestVoiceMode
2. Tester agent — dispatched as voice-tester, speaks scripted prompts
3. SIP participant bridges the tester agent to a real phone call
4. Twilio records both sides of the call
5. Recordings downloaded locally to logs/runs/<run_id>/

Flow:
  LiveKit Room
  ├── Phone Agent (TestVoiceMode) — listens, responds
  ├── Tester Agent (voice-tester) — speaks prompts, records what it hears
  └── SIP Participant — bridges to Twilio PSTN for recording

Usage:
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
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import yaml
from dotenv import load_dotenv
from livekit import api as lk_api

HARNESS_DIR = Path(__file__).parent
HARNESS_CONFIG = yaml.safe_load((HARNESS_DIR / "harness.yaml").read_text())
PHONE_AGENT_DIR = (HARNESS_DIR / HARNESS_CONFIG["phone_agent"]["path"]).resolve()
PHONE_LOGS_DIR = (HARNESS_DIR / HARNESS_CONFIG["phone_agent"]["session_logs"]).resolve()

load_dotenv(str((HARNESS_DIR / HARNESS_CONFIG["phone_agent"]["env_file"]).resolve()))
load_dotenv(str(HARNESS_DIR / ".env.local"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-20s %(message)s")
logger = logging.getLogger("phone-test")

# Config from env
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TEST_PHONE_NUMBER = os.environ.get("TEST_PHONE_NUMBER", "+17326552793")
AGENT_PHONE_NUMBER = os.environ.get("AGENT_PHONE_NUMBER", "+17328386479")
SIP_OUTBOUND_TRUNK = os.environ.get("SIP_OUTBOUND_TRUNK", "ST_9KtWBRmsXfG3")

PHONE_AGENT_NAME = "inbound-outbound"
TESTER_AGENT_NAME = "voice-tester"
RECORDINGS_DIR = HARNESS_DIR / "recordings"


async def run_phone_test(scenario_path: str, num_calls: int = 1, run_id: str | None = None):
    """Run end-to-end phone tests with real PSTN calls."""
    with open(scenario_path) as f:
        scenario = yaml.safe_load(f)

    run_id = run_id or f"phone_{scenario['name']}_{int(time.time())}"
    run_dir = Path("logs/runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    RECORDINGS_DIR.mkdir(exist_ok=True)

    # Load and apply agent config overrides
    config_path = HARNESS_DIR / "test_agent_config.json"
    agent_config = {}
    if config_path.exists():
        full_config = json.loads(config_path.read_text())
        agent_config = dict(full_config.get("agent", {}))
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

    logger.info(f"{'=' * 60}")
    logger.info(f"  Phone Test: {run_id}")
    logger.info(f"  Scenario: {scenario['name']} ({scenario.get('language', 'en')})")
    logger.info(f"  Calls: {num_calls}")
    logger.info(f"  SIP: Agent({AGENT_PHONE_NUMBER}) ↔ Caller({TEST_PHONE_NUMBER})")
    logger.info(f"{'=' * 60}")

    # Check tester agent is running
    logger.info("Ensure tester agent is running: cd tester && uv run python agent.py dev")

    lk_url = os.environ["LIVEKIT_URL"]
    lk_key = os.environ["LIVEKIT_API_KEY"]
    lk_secret = os.environ["LIVEKIT_API_SECRET"]

    all_results = []

    for i in range(num_calls):
        logger.info(f"\n--- Call {i+1}/{num_calls} ---")
        pre_logs = set(PHONE_LOGS_DIR.glob("*.json")) if PHONE_LOGS_DIR.exists() else set()

        call_dir = run_dir / f"call_{i+1}_{int(time.time())}"
        call_dir.mkdir(parents=True, exist_ok=True)

        result = await _run_call(
            lk_url, lk_key, lk_secret,
            scenario, agent_config, full_config,
            call_dir, i + 1,
        )

        # Wait for phone agent to finalize session log
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

        # Download Twilio recording
        if TWILIO_SID and TWILIO_TOKEN:
            _download_twilio_recordings(call_dir, result)

        (call_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))
        all_results.append(result)

        if i < num_calls - 1:
            logger.info("Pausing 10s between calls...")
            await asyncio.sleep(10)

    _report(all_results, scenario, run_dir)


async def _run_call(
    lk_url, lk_key, lk_secret,
    scenario, agent_config, full_config,
    call_dir, call_index,
) -> dict:
    """Run a single phone call."""
    conv_id = f"test-{uuid.uuid4().hex[:12]}"
    room_name = f"call-{conv_id}"

    lk = lk_api.LiveKitAPI(lk_url, lk_key, lk_secret)

    try:
        # 1. Create room
        await lk.room.create_room(lk_api.CreateRoomRequest(
            name=room_name, empty_timeout=180, max_participants=10,
        ))
        logger.info(f"[Call {call_index}] Room: {room_name}")

        # 2. Dispatch phone agent (TestVoiceMode — waits for participant)
        phone_metadata = json.dumps({
            "test": {
                "mode": "voice",
                "conversation_id": conv_id,
                "agent_id": agent_config.get("_id", "test"),
                "campaign_phone": "+18552563017",
                "local_agent_config": agent_config,
                "campaign_data": full_config.get("campaign"),
                "company_data": full_config.get("company"),
            }
        })
        await lk.agent_dispatch.create_dispatch(lk_api.CreateAgentDispatchRequest(
            room=room_name, agent_name=PHONE_AGENT_NAME, metadata=phone_metadata,
        ))
        logger.info(f"[Call {call_index}] Phone agent dispatched")

        # 3. Wait a moment then create SIP participant (bridges to Twilio PSTN)
        # This makes a real phone call from AGENT_PHONE_NUMBER to TEST_PHONE_NUMBER
        # Twilio answers on TEST_PHONE_NUMBER with TwiML (records the call)
        await asyncio.sleep(2)

        # Configure Twilio number to record when it answers
        if TWILIO_SID and TWILIO_TOKEN:
            _configure_twilio_recording(scenario)

        logger.info(f"[Call {call_index}] Creating SIP call {AGENT_PHONE_NUMBER} → {TEST_PHONE_NUMBER}...")
        call_start = time.time()

        await lk.sip.create_sip_participant(lk_api.CreateSIPParticipantRequest(
            room_name=room_name,
            sip_trunk_id=SIP_OUTBOUND_TRUNK,
            sip_call_to=TEST_PHONE_NUMBER,
            participant_identity="phone-caller",
        ))
        logger.info(f"[Call {call_index}] SIP participant created, call ringing...")

        # 4. Start Twilio recording once call connects
        await asyncio.sleep(5)  # Wait for call to connect
        if TWILIO_SID and TWILIO_TOKEN:
            try:
                from twilio.rest import Client as TwilioC
                tw = TwilioC(TWILIO_SID, TWILIO_TOKEN)
                # Find the active call to our test number
                active_calls = tw.calls.list(to=TEST_PHONE_NUMBER, status="in-progress", limit=1)
                if active_calls:
                    call_sid = active_calls[0].sid
                    tw.calls(call_sid).recordings.create(
                        recording_channels="dual",
                        recording_status_callback_event=["completed"],
                    )
                    logger.info(f"[Call {call_index}] Recording started on {call_sid}")
                    result_holder = {"twilio_call_sid": call_sid}
                else:
                    logger.warning(f"[Call {call_index}] No active Twilio call found to record")
                    result_holder = {}
            except Exception as e:
                logger.warning(f"[Call {call_index}] Recording start failed: {e}")
                result_holder = {}
        else:
            result_holder = {}

        # 5. Monitor call progress
        max_wait = 120
        for tick in range(max_wait // 5):
            await asyncio.sleep(5)
            elapsed = (tick + 1) * 5
            try:
                parts = await lk.room.list_participants(
                    lk_api.ListParticipantsRequest(room=room_name)
                )
                pcount = len(parts.participants) if parts.participants else 0
                names = [p.identity for p in (parts.participants or [])]
                if elapsed % 15 == 0:
                    logger.info(f"[Call {call_index}] {elapsed}s: {pcount} participants {names}")
                if elapsed > 20 and pcount <= 1:
                    logger.info(f"[Call {call_index}] Call ended ({elapsed}s)")
                    break
            except Exception:
                break

        call_duration = round(time.time() - call_start)

        # 5. Cleanup room
        try:
            await lk.room.delete_room(lk_api.DeleteRoomRequest(room=room_name))
        except Exception:
            pass

        return {
            "call_index": call_index,
            "room_name": room_name,
            "conv_id": conv_id,
            "duration_sec": call_duration,
            "twilio_call_sid": result_holder.get("twilio_call_sid"),
        }

    except Exception as e:
        logger.error(f"[Call {call_index}] Failed: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e), "call_index": call_index}
    finally:
        await lk.aclose()


def _configure_twilio_recording(scenario: dict):
    """Configure the Twilio test number to answer and record."""
    import urllib.parse
    from twilio.rest import Client

    client = Client(TWILIO_SID, TWILIO_TOKEN)

    # TwiML that answers, pauses (lets agent greeting play), then records
    language = scenario.get("language", "en")
    voice_map = {
        "en": "Polly.Matthew", "tr": "Polly.Filiz", "ar": "Polly.Zeina",
        "de": "Polly.Hans", "es": "Polly.Miguel", "fr": "Polly.Mathieu",
    }
    voice = voice_map.get(language, "Polly.Matthew")

    # Build TwiML with prompts
    parts = ['<Response>']
    wait = scenario.get("tester", {}).get("wait_after_greeting_sec", 4)
    if wait > 0:
        parts.append(f'<Pause length="{int(wait)}"/>')

    for prompt in scenario.get("prompts", []):
        parts.append(f'<Say voice="{voice}" language="{language}">{prompt["text"]}</Say>')
        pause = prompt.get("pause_after_sec", 4)
        if prompt.get("wait_for_response", True):
            parts.append(f'<Pause length="{int(pause)}"/>')

    parts.append('<Pause length="2"/><Hangup/></Response>')
    twiml = "".join(parts)

    echo_url = "https://twimlets.com/echo?Twiml=" + urllib.parse.quote(twiml)

    numbers = client.incoming_phone_numbers.list(phone_number=TEST_PHONE_NUMBER)
    if numbers:
        numbers[0].update(voice_url=echo_url, voice_method="GET")
        logger.info(f"Twilio {TEST_PHONE_NUMBER} configured with TwiML ({len(twiml)} chars)")


def _download_twilio_recordings(call_dir: Path, result: dict):
    """Download Twilio recording for this call."""
    from twilio.rest import Client

    call_sid = result.get("twilio_call_sid")
    if not call_sid:
        logger.warning("No Twilio call SID — can't download recording")
        return

    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)

        # Wait a bit for recording to finalize
        time.sleep(5)

        # Check for recordings on this specific call
        recordings = client.recordings.list(call_sid=call_sid, limit=1)
        if not recordings:
            logger.warning(f"No recording found for call {call_sid}")
            # Try listing all recent recordings
            all_recs = client.recordings.list(limit=5)
            if all_recs:
                logger.info(f"Found {len(all_recs)} other recordings — checking...")
                for r in all_recs:
                    if r.call_sid == call_sid:
                        recordings = [r]
                        break

        if not recordings:
            logger.warning("No recording available yet (may still be processing)")
            return

        rec = recordings[0]
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Recordings/{rec.sid}.mp3"
        auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()

        rec_path = call_dir / f"recording_{rec.sid}.mp3"
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req) as resp:
            rec_path.write_bytes(resp.read())

        # Also copy to recordings/ for easy access
        RECORDINGS_DIR.mkdir(exist_ok=True)
        conv_id = result.get("conv_id", "unknown")
        easy_path = RECORDINGS_DIR / f"{conv_id}.mp3"
        shutil.copy2(rec_path, easy_path)

        size = rec_path.stat().st_size
        logger.info(f"Recording saved: {rec_path} ({size} bytes)")
        logger.info(f"Easy access: {easy_path}")
        result["recording_path"] = str(rec_path)
        result["recording_easy_path"] = str(easy_path)
        result["recording_sid"] = rec.sid

    except Exception as e:
        logger.warning(f"Recording download failed: {e}")


def _extract_metrics(session_data: dict) -> dict:
    events = session_data.get("events", [])
    metrics = {}

    llm = [e for e in events if e.get("kind") == "llm" and e.get("ttft_ms")]
    if llm:
        ttfts = [e["ttft_ms"] for e in llm]
        metrics["llm_ttft_values"] = ttfts
        metrics["llm_ttft_avg"] = round(sum(ttfts) / len(ttfts))

    tts = [e for e in events if e.get("kind") == "tts" and e.get("ttfb_ms")]
    if tts:
        metrics["tts_ttfb_avg"] = round(sum(e["ttfb_ms"] for e in tts) / len(tts))

    aec = [e for e in events if e.get("kind") == "aec_first_input"]
    if aec:
        metrics["aec_first_input_sec"] = aec[0].get("elapsed_since_greeting_sec")
        metrics["aec_warmup_active"] = aec[0].get("aec_warmup_active")

    stt = [e for e in events if e.get("kind") == "stt_turn"]
    if stt:
        metrics["stt_turns"] = len(stt)
        metrics["phone_heard"] = " ".join(e.get("transcript", "") for e in stt)[:500]

    metrics["duration_sec"] = session_data.get("durationSec", 0)
    return metrics


def _report(results, scenario, run_dir):
    all_ttfts = []
    for r in results:
        sm = r.get("session_metrics", {})
        all_ttfts.extend(sm.get("llm_ttft_values", []))

    print()
    print(f"{'=' * 60}")
    print(f"  PHONE TEST: {scenario['name']}")
    print(f"  {sum(1 for r in results if 'error' not in r)}/{len(results)} calls")
    print(f"{'=' * 60}")

    if all_ttfts:
        all_ttfts.sort()
        print(f"  LLM TTFT avg:   {round(sum(all_ttfts)/len(all_ttfts))}ms")
        print(f"  LLM TTFT p50:   {all_ttfts[len(all_ttfts)//2]}ms")
        print(f"  LLM TTFT p90:   {all_ttfts[int(len(all_ttfts)*0.9)]}ms")

    for r in results:
        sm = r.get("session_metrics", {})
        rec = "yes" if r.get("recording_path") else "no"
        aec_sec = sm.get("aec_first_input_sec", "?")
        print(f"\n  Call {r.get('call_index', '?')}: dur={r.get('duration_sec', '?')}s rec={rec} aec_first_input={aec_sec}s")
        if sm.get("llm_ttft_values"):
            print(f"    TTFT per turn: {sm['llm_ttft_values']}")
        if sm.get("phone_heard"):
            print(f"    Heard: {sm['phone_heard'][:100]}...")
        if r.get("recording_path"):
            print(f"    Recording: {r['recording_path']}")

    print(f"\n  Results: {run_dir}/")
    print(f"  Recordings: {RECORDINGS_DIR}/")
    print(f"{'=' * 60}")

    agg = {"scenario": scenario["name"], "results": results}
    if all_ttfts:
        agg["llm_ttft_avg"] = round(sum(all_ttfts) / len(all_ttfts))
    (run_dir / "aggregate.json").write_text(json.dumps(agg, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description="End-to-end phone test")
    parser.add_argument("scenario", help="Scenario YAML")
    parser.add_argument("--calls", type=int, default=1)
    parser.add_argument("--run-id")
    args = parser.parse_args()

    if not Path(args.scenario).exists():
        print(f"Scenario not found: {args.scenario}")
        sys.exit(1)

    asyncio.run(run_phone_test(args.scenario, num_calls=args.calls, run_id=args.run_id))


if __name__ == "__main__":
    main()
