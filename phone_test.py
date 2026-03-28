"""
End-to-End Phone Test — Two LiveKit Projects

Two separate LiveKit projects bridged by a real phone call:

  e2e-outbound (Room A)          e2e-inbound (Room B)
  ┌──────────────────┐           ┌──────────────────┐
  │  Phone Agent     │  ← PSTN → │  Tester Agent    │
  │  (inbound-outbound)│           │  (voice-tester)  │
  └──────────────────┘           └──────────────────┘
        │                              │
   +17328386479                  +17329441794
   (Twilio outbound)            (LiveKit native inbound)

Prerequisites:
    # Terminal 1: Phone agent on e2e-outbound
    LIVEKIT_URL=wss://e2e-outbound-o6f8ohvn.livekit.cloud \\
    LIVEKIT_API_KEY=APIc7fUiT2azPhr \\
    LIVEKIT_API_SECRET=kcuIHMzW8MSnGeahWzBFSFgcvR5yfHhFlMExEeJWYJMA \\
    uv run src/main.py dev

    # Terminal 2: Tester agent on e2e-inbound
    cd tester && uv run python agent.py dev

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

# Load E2E config
E2E_CONFIG = json.loads((HARNESS_DIR / "e2e_config.json").read_text())
OUTBOUND = E2E_CONFIG["outbound_project"]
INBOUND = E2E_CONFIG["inbound_project"]

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
RECORDINGS_DIR = HARNESS_DIR / "recordings"


async def run_phone_test(scenario_path: str, num_calls: int = 1, run_id: str | None = None):
    with open(scenario_path) as f:
        scenario = yaml.safe_load(f)

    run_id = run_id or f"phone_{scenario['name']}_{int(time.time())}"
    run_dir = Path("logs/runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    RECORDINGS_DIR.mkdir(exist_ok=True)

    # Load and override agent config
    config_path = HARNESS_DIR / "test_agent_config.json"
    full_config = json.loads(config_path.read_text()) if config_path.exists() else {}
    agent_config = dict(full_config.get("agent", {}))
    _apply_overrides(agent_config, scenario)

    # Write active scenario for tester agent
    shutil.copy2(scenario_path, HARNESS_DIR / "active_scenario.yaml")

    logger.info(f"{'=' * 60}")
    logger.info(f"  E2E Phone Test: {run_id}")
    logger.info(f"  Scenario: {scenario['name']} ({scenario.get('language', 'en')})")
    logger.info(f"  Calls: {num_calls}")
    logger.info(f"  Phone agent ({OUTBOUND['twilio_number']}) → PSTN → Tester ({INBOUND['livekit_number']})")
    logger.info(f"{'=' * 60}")

    all_results = []

    for i in range(num_calls):
        logger.info(f"\n--- Call {i+1}/{num_calls} ---")
        pre_logs = set(PHONE_LOGS_DIR.glob("*.json")) if PHONE_LOGS_DIR.exists() else set()

        call_dir = run_dir / f"call_{i+1}_{int(time.time())}"
        call_dir.mkdir(parents=True, exist_ok=True)

        result = await _run_call(scenario, agent_config, full_config, call_dir, i + 1)

        # Wait for session logs
        await asyncio.sleep(12)

        # Copy phone agent session log
        if PHONE_LOGS_DIR.exists():
            post_logs = set(PHONE_LOGS_DIR.glob("*.json"))
            new_logs = sorted(post_logs - pre_logs, key=lambda f: f.stat().st_mtime, reverse=True)
            if new_logs:
                shutil.copy2(new_logs[0], call_dir / "phone_session.json")
                logger.info(f"Copied phone session log: {new_logs[0].name}")
                result["session_metrics"] = _extract_metrics(json.loads(new_logs[0].read_text()))

        # Download recording from S3
        if result.get("egress_id"):
            await asyncio.sleep(5)  # Wait for egress to finalize
            _download_s3_recording(call_dir, result)

        (call_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))
        all_results.append(result)

        if i < num_calls - 1:
            logger.info("Pausing 10s between calls...")
            await asyncio.sleep(10)

    _report(all_results, scenario, run_dir)


async def _run_call(scenario, agent_config, full_config, call_dir, call_index) -> dict:
    conv_id = f"test-{uuid.uuid4().hex[:12]}"
    room_name = f"call-{conv_id}"

    # Connect to e2e-outbound project (where phone agent lives)
    lk = lk_api.LiveKitAPI(OUTBOUND["url"], OUTBOUND["api_key"], OUTBOUND["api_secret"])

    try:
        # 1. Create Room A on outbound project
        await lk.room.create_room(lk_api.CreateRoomRequest(
            name=room_name, empty_timeout=180, max_participants=10,
        ))
        logger.info(f"[Call {call_index}] Room A: {room_name} (e2e-outbound)")

        # 2. Dispatch phone agent to Room A
        metadata = json.dumps({
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
            room=room_name, agent_name="inbound-outbound", metadata=metadata,
        ))
        logger.info(f"[Call {call_index}] Phone agent dispatched")
        await asyncio.sleep(3)

        # 3. Create SIP participant → calls tester's LiveKit native number
        #    e2e-outbound → Twilio PSTN → +17329441794 → e2e-inbound → tester agent
        call_start = time.time()
        logger.info(f"[Call {call_index}] Calling {INBOUND['livekit_number']}...")

        await lk.sip.create_sip_participant(lk_api.CreateSIPParticipantRequest(
            room_name=room_name,
            sip_trunk_id=OUTBOUND["sip_outbound_trunk"],
            sip_call_to=INBOUND["livekit_number"],
            participant_identity="sip-bridge",
        ))
        logger.info(f"[Call {call_index}] SIP call connected")

        # 4. Try to start Twilio recording
        twilio_call_sid = None
        await asyncio.sleep(5)
        # Start LiveKit room composite egress for recording (audio only, to S3)
        egress_id = None
        try:
            from livekit.protocol.egress import (
                RoomCompositeEgressRequest, EncodedFileOutput, EncodedFileType, S3Upload
            )
            s3_config = S3Upload(
                access_key=os.environ.get("AWS_ACCESS_KEY_ID", ""),
                secret=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
                bucket=os.environ.get("AWS_S3_BUCKET", "local-convex-testing"),
                region=os.environ.get("AWS_REGION", "us-east-1"),
            )
            egress_req = RoomCompositeEgressRequest(
                room_name=room_name,
                audio_only=True,
                file_outputs=[EncodedFileOutput(
                    file_type=EncodedFileType.OGG,
                    filepath=f"test-recordings/{conv_id}.ogg",
                    s3=s3_config,
                )],
            )
            egress = await lk.egress.start_room_composite_egress(egress_req)
            egress_id = egress.egress_id
            logger.info(f"[Call {call_index}] Recording egress started: {egress_id}")
        except Exception as e:
            logger.warning(f"[Call {call_index}] Egress recording failed: {e}")

        # 5. Monitor call
        for tick in range(30):  # up to 2.5 min
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
                if elapsed > 30 and pcount <= 1:
                    logger.info(f"[Call {call_index}] Call ended ({elapsed}s)")
                    break
            except Exception:
                break

        call_duration = round(time.time() - call_start)

        # Stop egress recording
        if egress_id:
            try:
                await lk.egress.stop_egress(lk_api.StopEgressRequest(egress_id=egress_id))
                logger.info(f"[Call {call_index}] Recording egress stopped")
            except Exception as e:
                logger.debug(f"[Call {call_index}] Egress stop: {e}")

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
            "egress_id": egress_id,
        }

    except Exception as e:
        logger.error(f"[Call {call_index}] Failed: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e), "call_index": call_index}
    finally:
        await lk.aclose()


def _apply_overrides(agent_config, scenario):
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


def _download_s3_recording(call_dir, result):
    """Download recording from S3 after egress completes."""
    conv_id = result.get("conv_id", "unknown")
    s3_key = f"test-recordings/{conv_id}.ogg"
    try:
        import boto3
        s3 = boto3.client(
            "s3",
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        bucket = os.environ.get("AWS_S3_BUCKET", "local-convex-testing")
        rec_path = call_dir / f"recording_{conv_id}.ogg"
        s3.download_file(bucket, s3_key, str(rec_path))

        RECORDINGS_DIR.mkdir(exist_ok=True)
        easy = RECORDINGS_DIR / f"{conv_id}.ogg"
        shutil.copy2(rec_path, easy)
        logger.info(f"Recording downloaded: {rec_path} ({rec_path.stat().st_size} bytes)")
        logger.info(f"Easy access: {easy}")
        result["recording_path"] = str(rec_path)
    except Exception as e:
        logger.warning(f"S3 recording download failed: {e}")


def _download_recording(call_dir, result):
    if not TWILIO_SID:
        return
    call_sid = result.get("twilio_call_sid")
    if not call_sid:
        return
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        time.sleep(5)

        recordings = client.recordings.list(call_sid=call_sid, limit=1)
        if not recordings:
            children = client.calls.list(parent_call_sid=call_sid, limit=3)
            for ch in children:
                recordings = client.recordings.list(call_sid=ch.sid, limit=1)
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
            result["recording_path"] = str(rec_path)
        else:
            logger.warning("No recording found")
    except Exception as e:
        logger.warning(f"Recording download: {e}")


def _extract_metrics(data):
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
    print(f"  E2E PHONE TEST: {scenario['name']}")
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
            print(f"    Heard: {sm['phone_heard'][:120]}...")
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
    parser = argparse.ArgumentParser(description="E2E phone test (two LiveKit projects)")
    parser.add_argument("scenario", help="Scenario YAML")
    parser.add_argument("--calls", type=int, default=1)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if not Path(args.scenario).exists():
        sys.exit(f"Scenario not found: {args.scenario}")
    asyncio.run(run_phone_test(args.scenario, num_calls=args.calls, run_id=args.run_id))


if __name__ == "__main__":
    main()
