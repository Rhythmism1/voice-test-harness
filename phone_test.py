"""
End-to-End Phone Test — Two Room Architecture

Two separate LiveKit rooms bridged by a real phone call:

  Room A (phone agent)  ←→  Twilio PSTN  ←→  Room B (tester agent)

Flow:
1. Create Room A, dispatch phone agent (TestVoiceMode)
2. Room A's SIP participant calls +17326552793 (outbound trunk)
3. Twilio answers, <Dial>s +17328386479
4. +17328386479 hits LiveKit inbound trunk → dispatch rule creates Room B
5. Tester agent (voice-tester) dispatched to Room B via dispatch rule
6. Phone agent and tester agent talk through real PSTN
7. Twilio records the call (dual channel)
8. Recording downloaded locally

Prerequisites:
- Phone agent running: cd ../phone && uv run src/main.py dev
- Tester agent running: cd tester && uv run python agent.py dev

Usage:
    uv run python phone_test.py scenarios/refund_call.yaml
    uv run python phone_test.py scenarios/refund_call.yaml --calls 2
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
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import yaml
from dotenv import load_dotenv
from livekit import api as lk_api

HARNESS_DIR = Path(__file__).parent
HARNESS_CONFIG = yaml.safe_load((HARNESS_DIR / "harness.yaml").read_text())
PHONE_LOGS_DIR = (HARNESS_DIR / HARNESS_CONFIG["phone_agent"]["session_logs"]).resolve()

load_dotenv(str((HARNESS_DIR / HARNESS_CONFIG["phone_agent"]["env_file"]).resolve()))
load_dotenv(str(HARNESS_DIR / ".env.local"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-20s %(message)s")
logger = logging.getLogger("phone-test")

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TEST_PHONE_NUMBER = os.environ.get("TEST_PHONE_NUMBER", "+17326552793")
AGENT_PHONE_NUMBER = os.environ.get("AGENT_PHONE_NUMBER", "+17328386479")
SIP_OUTBOUND_TRUNK = os.environ.get("SIP_OUTBOUND_TRUNK", "ST_9KtWBRmsXfG3")

PHONE_AGENT_NAME = "inbound-outbound"
RECORDINGS_DIR = HARNESS_DIR / "recordings"


async def run_phone_test(scenario_path: str, num_calls: int = 1, run_id: str | None = None):
    with open(scenario_path) as f:
        scenario = yaml.safe_load(f)

    run_id = run_id or f"phone_{scenario['name']}_{int(time.time())}"
    run_dir = Path("logs/runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    RECORDINGS_DIR.mkdir(exist_ok=True)

    # Load agent config
    config_path = HARNESS_DIR / "test_agent_config.json"
    full_config = json.loads(config_path.read_text()) if config_path.exists() else {}
    agent_config = dict(full_config.get("agent", {}))
    _apply_overrides(agent_config, scenario)

    # Write active scenario for the tester agent (reads it on inbound dispatch)
    active_path = HARNESS_DIR / "active_scenario.yaml"
    shutil.copy2(scenario_path, active_path)
    logger.info(f"Active scenario written for tester agent")

    # No Twilio bridge needed — two separate LiveKit projects
    # Outbound: phone agent calls +17326552793 via SIP
    # Twilio receives on +17326552793, SIP trunk origination routes to e2e-inbound LiveKit
    # Inbound: dispatch rule creates room, tester agent dispatched

    logger.info(f"{'=' * 60}")
    logger.info(f"  Phone Test: {run_id}")
    logger.info(f"  Scenario: {scenario['name']} ({scenario.get('language', 'en')})")
    logger.info(f"  Calls: {num_calls}")
    logger.info(f"  Room A (phone agent) → {TEST_PHONE_NUMBER} → Twilio → {AGENT_PHONE_NUMBER} → Room B (tester)")
    logger.info(f"{'=' * 60}")

    all_results = []

    for i in range(num_calls):
        logger.info(f"\n--- Call {i+1}/{num_calls} ---")
        pre_logs = set(PHONE_LOGS_DIR.glob("*.json")) if PHONE_LOGS_DIR.exists() else set()

        call_dir = run_dir / f"call_{i+1}_{int(time.time())}"
        call_dir.mkdir(parents=True, exist_ok=True)

        result = await _run_call(scenario, agent_config, full_config, call_dir, i + 1, scenario_path)

        # Wait for session logs
        await asyncio.sleep(10)

        # Copy phone agent session log
        if PHONE_LOGS_DIR.exists():
            post_logs = set(PHONE_LOGS_DIR.glob("*.json"))
            new_logs = sorted(post_logs - pre_logs, key=lambda f: f.stat().st_mtime, reverse=True)
            if new_logs:
                shutil.copy2(new_logs[0], call_dir / "phone_session.json")
                logger.info(f"Copied session log: {new_logs[0].name}")
                result["session_metrics"] = _extract_metrics(json.loads(new_logs[0].read_text()))

        # Download recording
        _download_recording(call_dir, result)

        (call_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))
        all_results.append(result)

        if i < num_calls - 1:
            logger.info("Pausing 10s between calls...")
            await asyncio.sleep(10)

    _report(all_results, scenario, run_dir)


async def _run_call(scenario, agent_config, full_config, call_dir, call_index, scenario_path) -> dict:
    lk_url = os.environ["LIVEKIT_URL"]
    lk_key = os.environ["LIVEKIT_API_KEY"]
    lk_secret = os.environ["LIVEKIT_API_SECRET"]

    conv_id = f"test-{uuid.uuid4().hex[:12]}"
    room_name = f"call-{conv_id}"

    lk = lk_api.LiveKitAPI(lk_url, lk_key, lk_secret)

    try:
        # 1. Create Room A for phone agent
        await lk.room.create_room(lk_api.CreateRoomRequest(
            name=room_name, empty_timeout=180, max_participants=10,
        ))
        logger.info(f"[Call {call_index}] Room A: {room_name}")

        # 2. Dispatch phone agent to Room A (TestVoiceMode)
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
        logger.info(f"[Call {call_index}] Phone agent dispatched to Room A")
        await asyncio.sleep(2)

        # 3. Create SIP participant in Room A → calls +17326552793
        #    Twilio answers → <Dial>s +17328386479 → LiveKit inbound → Room B
        #    Tester agent auto-dispatched to Room B via dispatch rule
        call_start = time.time()
        logger.info(f"[Call {call_index}] SIP: Room A → {TEST_PHONE_NUMBER} → Twilio → {AGENT_PHONE_NUMBER} → Room B")
        await lk.sip.create_sip_participant(lk_api.CreateSIPParticipantRequest(
            room_name=room_name,
            sip_trunk_id=SIP_OUTBOUND_TRUNK,
            sip_call_to=TEST_PHONE_NUMBER,
            participant_identity="sip-bridge",
        ))
        logger.info(f"[Call {call_index}] SIP call initiated")

        # 4. Start Twilio recording
        await asyncio.sleep(10)
        twilio_call_sid = None
        if TWILIO_SID and TWILIO_TOKEN:
            from twilio.rest import Client
            tw = Client(TWILIO_SID, TWILIO_TOKEN)
            active = tw.calls.list(to=TEST_PHONE_NUMBER, status="in-progress", limit=1)
            if active:
                twilio_call_sid = active[0].sid
                try:
                    tw.calls(twilio_call_sid).recordings.create(recording_channels="dual")
                    logger.info(f"[Call {call_index}] Recording started: {twilio_call_sid}")
                except Exception as e:
                    logger.warning(f"[Call {call_index}] Recording failed: {e}")

        # 5. Monitor call
        for tick in range(24):  # up to 2 min
            await asyncio.sleep(5)
            elapsed = (tick + 1) * 5
            try:
                parts = await lk.room.list_participants(
                    lk_api.ListParticipantsRequest(room=room_name)
                )
                pcount = len(parts.participants) if parts.participants else 0
                if elapsed % 15 == 0:
                    names = [p.identity for p in (parts.participants or [])]
                    logger.info(f"[Call {call_index}] {elapsed}s: {pcount} participants {names}")
                if elapsed > 20 and pcount <= 1:
                    logger.info(f"[Call {call_index}] Call ended ({elapsed}s)")
                    break
            except Exception:
                break

        call_duration = round(time.time() - call_start)

        try:
            await lk.room.delete_room(lk_api.DeleteRoomRequest(room=room_name))
        except Exception:
            pass

        return {
            "call_index": call_index,
            "room_name": room_name,
            "conv_id": conv_id,
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


def _apply_overrides(agent_config: dict, scenario: dict):
    overrides = dict(scenario.get("agent_overrides", {}))
    if "instructions_file" in overrides:
        ipath = HARNESS_DIR / overrides.pop("instructions_file")
        if ipath.exists():
            overrides["instructions"] = ipath.read_text()
    for key, val in overrides.items():
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


def _configure_twilio_bridge():
    """Configure +17326552793 to <Dial> +17328386479 with recording."""
    if not TWILIO_SID or not TWILIO_TOKEN:
        return
    from twilio.rest import Client
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    twiml = f'<Response><Dial timeout="60" record="record-from-answer-dual">{AGENT_PHONE_NUMBER}</Dial></Response>'
    echo_url = "https://twimlets.com/echo?Twiml=" + urllib.parse.quote(twiml)
    numbers = client.incoming_phone_numbers.list(phone_number=TEST_PHONE_NUMBER)
    if numbers:
        numbers[0].update(voice_url=echo_url, voice_method="GET")
        logger.info(f"Twilio bridge: {TEST_PHONE_NUMBER} → <Dial> → {AGENT_PHONE_NUMBER}")


def _download_recording(call_dir: Path, result: dict):
    call_sid = result.get("twilio_call_sid")
    if not call_sid or not TWILIO_SID:
        return

    from twilio.rest import Client
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        time.sleep(5)
        recordings = client.recordings.list(call_sid=call_sid, limit=1)
        if not recordings:
            # Check child calls (the <Dial> creates a child call)
            call = client.calls(call_sid).fetch()
            child_calls = client.calls.list(parent_call_sid=call_sid, limit=3)
            for child in child_calls:
                recordings = client.recordings.list(call_sid=child.sid, limit=1)
                if recordings:
                    break

        if recordings:
            rec = recordings[0]
            url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Recordings/{rec.sid}.mp3"
            auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
            rec_path = call_dir / f"recording_{rec.sid}.mp3"
            req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
            with urllib.request.urlopen(req) as resp:
                rec_path.write_bytes(resp.read())

            RECORDINGS_DIR.mkdir(exist_ok=True)
            easy = RECORDINGS_DIR / f"{result.get('conv_id', 'unknown')}.mp3"
            shutil.copy2(rec_path, easy)
            logger.info(f"Recording: {rec_path} ({rec_path.stat().st_size} bytes)")
            logger.info(f"Easy access: {easy}")
            result["recording_path"] = str(rec_path)
        else:
            logger.warning("No recording found")
    except Exception as e:
        logger.warning(f"Recording download failed: {e}")


def _extract_metrics(data: dict) -> dict:
    events = data.get("events", [])
    m = {}
    llm = [e for e in events if e.get("kind") == "llm" and e.get("ttft_ms")]
    if llm:
        m["llm_ttft_values"] = [e["ttft_ms"] for e in llm]
        m["llm_ttft_avg"] = round(sum(m["llm_ttft_values"]) / len(m["llm_ttft_values"]))
    tts = [e for e in events if e.get("kind") == "tts" and e.get("ttfb_ms")]
    if tts:
        m["tts_ttfb_avg"] = round(sum(e["ttfb_ms"] for e in tts) / len(tts))
    aec = [e for e in events if e.get("kind") == "aec_first_input"]
    if aec:
        m["aec_first_input_sec"] = aec[0].get("elapsed_since_greeting_sec")
    stt = [e for e in events if e.get("kind") == "stt_turn"]
    if stt:
        m["phone_heard"] = " ".join(e.get("transcript", "") for e in stt)[:500]
    m["duration_sec"] = data.get("durationSec", 0)
    return m


def _report(results, scenario, run_dir):
    all_ttfts = []
    for r in results:
        all_ttfts.extend(r.get("session_metrics", {}).get("llm_ttft_values", []))

    print(f"\n{'=' * 60}")
    print(f"  PHONE TEST: {scenario['name']}")
    print(f"  {sum(1 for r in results if 'error' not in r)}/{len(results)} calls")
    print(f"{'=' * 60}")
    if all_ttfts:
        all_ttfts.sort()
        print(f"  LLM TTFT avg:  {round(sum(all_ttfts)/len(all_ttfts))}ms")
        print(f"  LLM TTFT p50:  {all_ttfts[len(all_ttfts)//2]}ms")
        print(f"  LLM TTFT p90:  {all_ttfts[int(len(all_ttfts)*0.9)]}ms")
    for r in results:
        sm = r.get("session_metrics", {})
        rec = "yes" if r.get("recording_path") else "no"
        print(f"\n  Call {r.get('call_index')}: dur={r.get('duration_sec')}s rec={rec}")
        if sm.get("llm_ttft_values"):
            print(f"    TTFT: {sm['llm_ttft_values']}")
        if sm.get("phone_heard"):
            print(f"    Heard: {sm['phone_heard'][:100]}...")
        if r.get("recording_path"):
            print(f"    Recording: {r['recording_path']}")
    print(f"\n  Results: {run_dir}/")
    print(f"  Recordings: {RECORDINGS_DIR}/")
    print(f"{'=' * 60}\n")

    (run_dir / "aggregate.json").write_text(json.dumps(
        {"scenario": scenario["name"], "results": results,
         "llm_ttft_avg": round(sum(all_ttfts)/len(all_ttfts)) if all_ttfts else None},
        indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description="End-to-end phone test (two-room)")
    parser.add_argument("scenario", help="Scenario YAML")
    parser.add_argument("--calls", type=int, default=1)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if not Path(args.scenario).exists():
        sys.exit(f"Scenario not found: {args.scenario}")
    asyncio.run(run_phone_test(args.scenario, num_calls=args.calls, run_id=args.run_id))


if __name__ == "__main__":
    main()
