"""
Test Run Orchestrator

Creates a LiveKit room, dispatches the phone agent via LiveKit's dispatch API,
joins as a participant, speaks scripted prompts, records responses, and analyzes.

The phone agent must be running in dev mode (`uv run src/main.py dev`) separately.
This script dispatches jobs TO it — it does not start it.

Usage:
    # First, in another terminal:
    cd ../phone && uv run src/main.py dev

    # Then run a test:
    uv run python run.py scenarios/basic_english.yaml
    uv run python run.py scenarios/basic_english.yaml --run-id my_test
    uv run python run.py scenarios/basic_english.yaml --calls 3
"""

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml
from dotenv import load_dotenv
from livekit import api as lk_api, rtc

# Load harness config
HARNESS_DIR = Path(__file__).parent
HARNESS_CONFIG = yaml.safe_load((HARNESS_DIR / "harness.yaml").read_text())
PHONE_LOGS_DIR = (HARNESS_DIR / HARNESS_CONFIG["phone_agent"]["session_logs"]).resolve()

# Load env
load_dotenv(str((HARNESS_DIR / HARNESS_CONFIG["phone_agent"]["env_file"]).resolve()))
load_dotenv(str(HARNESS_DIR / ".env.local"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-20s %(message)s")
logger = logging.getLogger("orchestrator")

# Agent config — must match what's in Convex
AGENT_NAME = "inbound-outbound"
DEFAULT_AGENT_ID = "j57dwty1na1smcfebtprmzbedh83phqe"
DEFAULT_CAMPAIGN_PHONE = "+18552563017"


async def run_single_call(
    scenario: dict,
    run_dir: Path,
    call_index: int,
    agent_id: str,
    campaign_phone: str,
) -> dict:
    """Run a single test call. Returns call result dict."""
    call_id = f"call_{call_index}_{int(time.time())}"
    call_dir = run_dir / call_id
    call_dir.mkdir(parents=True, exist_ok=True)

    lk_url = os.environ["LIVEKIT_URL"]
    lk_key = os.environ["LIVEKIT_API_KEY"]
    lk_secret = os.environ["LIVEKIT_API_SECRET"]

    # Create a fake conversation ID for room naming
    conv_id = f"test-{uuid.uuid4().hex[:20]}"
    room_name = f"call-{conv_id}"

    logger.info(f"[Call {call_index}] Room: {room_name}")

    # Snapshot existing session logs
    pre_logs = set(PHONE_LOGS_DIR.glob("*.json")) if PHONE_LOGS_DIR.exists() else set()

    lk = lk_api.LiveKitAPI(lk_url, lk_key, lk_secret)

    try:
        # 1. Create room
        await lk.room.create_room(lk_api.CreateRoomRequest(
            name=room_name,
            empty_timeout=300,
            max_participants=10,
        ))
        logger.info(f"[Call {call_index}] Room created")

        # 2. Dispatch phone agent with full config overrides (avoids Convex fetch)
        config_path = HARNESS_DIR / "test_agent_config.json"
        overrides = {}
        if config_path.exists():
            full_config = json.loads(config_path.read_text())
            agent_config = dict(full_config.get("agent", {}))

            # Apply scenario-level agent overrides
            agent_overrides = dict(scenario.get("agent_overrides", {}))

            # Support loading instructions from file
            if "instructions_file" in agent_overrides:
                instructions_path = HARNESS_DIR / agent_overrides.pop("instructions_file")
                if instructions_path.exists():
                    agent_overrides["instructions"] = instructions_path.read_text()
                    logger.info(f"[Call {call_index}] Loaded instructions from {instructions_path.name} ({len(agent_overrides['instructions'])} chars)")
            for key, val in agent_overrides.items():
                if key == "config" and isinstance(val, dict):
                    # Deep merge into agent.config
                    cfg = dict(agent_config.get("config", {}))
                    for ck, cv in val.items():
                        if isinstance(cv, dict) and isinstance(cfg.get(ck), dict):
                            cfg[ck] = {**cfg[ck], **cv}
                        else:
                            cfg[ck] = cv
                    agent_config["config"] = cfg
                else:
                    # Top-level override (instructions, personality, name, etc.)
                    agent_config[key] = val

            if agent_overrides:
                logger.info(f"[Call {call_index}] Applied agent overrides: {list(agent_overrides.keys())}")

            overrides = {
                "local_agent_config": agent_config,
                "campaign_data": full_config.get("campaign"),
                "company_data": full_config.get("company"),
            }

        metadata = json.dumps({
            "test": {
                "mode": "voice",
                "conversation_id": conv_id,
                "agent_id": agent_id,
                "campaign_phone": campaign_phone,
                **overrides,
            }
        })

        dispatch = await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(
                room=room_name,
                agent_name=AGENT_NAME,
                metadata=metadata,
            )
        )
        logger.info(f"[Call {call_index}] Agent dispatched: {dispatch.id if hasattr(dispatch, 'id') else 'ok'}")

        # 3. Generate participant token for tester
        token = lk_api.AccessToken(lk_key, lk_secret) \
            .with_identity(f"tester-{conv_id}") \
            .with_grants(lk_api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
            ))
        tester_token = token.to_jwt()

        # 4. Join room as participant and run the test
        call_result = await _run_call(
            lk_url, tester_token, room_name, scenario, call_index, call_dir
        )

        # 5. Wait for phone agent to finalize and write session log
        # Phone agent needs ~5-8s after disconnect to finalize (end_call, metrics, session log)
        await asyncio.sleep(10)

        # 6. Find and copy new session logs
        if PHONE_LOGS_DIR.exists():
            post_logs = set(PHONE_LOGS_DIR.glob("*.json"))
            new_logs = sorted(post_logs - pre_logs, key=lambda f: f.stat().st_mtime, reverse=True)
            if new_logs:
                shutil.copy2(new_logs[0], call_dir / "phone_session.json")
                logger.info(f"[Call {call_index}] Copied session log: {new_logs[0].name}")

                # Extract metrics from session log
                session_data = json.loads(new_logs[0].read_text())
                call_result["session_metrics"] = _extract_session_metrics(session_data)
            else:
                logger.warning(f"[Call {call_index}] No new session log found")

        # 7. Delete room
        try:
            await lk.room.delete_room(lk_api.DeleteRoomRequest(room=room_name))
        except Exception:
            pass

        # Save call result
        (call_dir / "result.json").write_text(json.dumps(call_result, indent=2, default=str))
        return call_result

    except Exception as e:
        logger.error(f"[Call {call_index}] Failed: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e), "call_index": call_index}
    finally:
        await lk.aclose()


async def _monitor_agent_audio(stream: rtc.AudioStream, event: asyncio.Event, call_index: int):
    """Monitor agent audio stream for non-silent frames.

    Sets the event whenever we detect non-silent audio after a period of silence.
    This detects when the agent starts speaking (greeting or response).
    """
    import numpy as np

    SILENCE_THRESHOLD = 200  # RMS threshold — below this is silence
    was_silent = True

    try:
        async for frame_event in stream:
            frame = frame_event.frame
            # Check RMS energy of the audio frame
            samples = np.frombuffer(frame.data, dtype=np.int16)
            rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2)) if len(samples) > 0 else 0

            if rms > SILENCE_THRESHOLD:
                if was_silent:
                    # Transition from silence to speech — agent started talking
                    event.set()
                    was_silent = False
            else:
                was_silent = True
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug(f"[Call {call_index}] Audio monitor error: {e}")


async def _run_call(
    lk_url: str,
    token: str,
    room_name: str,
    scenario: dict,
    call_index: int,
    call_dir: Path,
) -> dict:
    """Join room, speak prompts, record responses.

    Replicates the real inbound call flow:
    1. Tester joins room (like a SIP participant connecting)
    2. Agent joins, starts greeting + AEC warmup (3s)
    3. Tester detects agent audio (greeting started)
    4. Tester speaks first prompt — may land during AEC warmup
    5. Tester waits until it detects agent audio again (response)
    6. Measures wall-clock time from end-of-speak to agent-audio-detected

    No hardcoded sleeps for timing — all event-driven.
    """
    from livekit.plugins import cartesia

    room = rtc.Room()
    prompts = scenario.get("prompts", [])
    call_log = []
    http_session = None

    # Track when agent audio activity is detected
    agent_audio_detected = asyncio.Event()
    _agent_audio_stream = [None]  # will hold the AudioStream for frame monitoring
    _audio_monitor_task = [None]

    @room.on("track_subscribed")
    def on_track_sub(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            logger.info(f"[Call {call_index}] Agent audio track subscribed")
            # Start monitoring audio frames on this track
            stream = rtc.AudioStream(track)
            _agent_audio_stream[0] = stream
            _audio_monitor_task[0] = asyncio.create_task(
                _monitor_agent_audio(stream, agent_audio_detected, call_index)
            )

    try:
        # 1. Connect to room
        await room.connect(lk_url, token)
        logger.info(f"[Call {call_index}] Connected to room")
        connect_time = time.time()

        # 2. Wait for agent to join
        agent_participant = None
        if room.remote_participants:
            agent_participant = list(room.remote_participants.values())[0]
        else:
            join_fut = asyncio.Future()

            @room.on("participant_connected")
            def on_join(p):
                if not join_fut.done():
                    join_fut.set_result(p)

            try:
                agent_participant = await asyncio.wait_for(join_fut, 20)
            except asyncio.TimeoutError:
                logger.error(f"[Call {call_index}] Agent never joined")
                return {"error": "agent_timeout", "call_index": call_index}

        agent_join_time = time.time()
        logger.info(
            f"[Call {call_index}] Agent joined: {agent_participant.identity} "
            f"({round((agent_join_time - connect_time) * 1000)}ms after connect)"
        )

        # 3. Publish our audio track
        audio_source = rtc.AudioSource(sample_rate=24000, num_channels=1)
        track = rtc.LocalAudioTrack.create_audio_track("tester-mic", audio_source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

        # 4. Wait for agent's greeting audio to start
        # This is event-driven — no hardcoded sleep. We detect when the agent
        # publishes its audio track (greeting TTS starts playing).
        try:
            await asyncio.wait_for(agent_audio_detected.wait(), 15)
            greeting_detected_time = time.time()
            logger.info(
                f"[Call {call_index}] Agent greeting detected "
                f"({round((greeting_detected_time - agent_join_time) * 1000)}ms after agent join)"
            )
        except asyncio.TimeoutError:
            logger.warning(f"[Call {call_index}] No agent audio detected in 15s, proceeding anyway")
            greeting_detected_time = time.time()

        # 5. Configurable wait after greeting detected before speaking
        # Default: 0s (speak immediately, like a real caller saying "hello" during greeting)
        # Set higher to skip AEC window for baseline comparison
        post_greeting_wait = scenario.get("tester", {}).get("wait_after_greeting_sec", 0)
        if post_greeting_wait > 0:
            logger.info(f"[Call {call_index}] Waiting {post_greeting_wait}s after greeting...")
            await asyncio.sleep(post_greeting_wait)

        # 6. Set up TTS
        import aiohttp
        http_session = aiohttp.ClientSession()
        tts = cartesia.TTS(
            voice=scenario.get("tester", {}).get(
                "voice", "a0e99841-438c-4a64-b679-ae501e7d6091"
            ),
            http_session=http_session,
        )

        # 7. Run each prompt
        for i, prompt in enumerate(prompts):
            text = prompt["text"]
            turn_start = time.time()
            logger.info(f"[Call {call_index}] Prompt {i+1}/{len(prompts)}: {text[:60]}...")

            # Reset agent audio detection for this turn
            agent_audio_detected.clear()

            # Synthesize and send
            async for ev in tts.synthesize(text):
                await audio_source.capture_frame(ev.frame)

            speak_end = time.time()

            # Wait for agent to respond — either detect audio or timeout
            pause = prompt.get("pause_after_sec", 4.0)
            if prompt.get("wait_for_response", True):
                try:
                    await asyncio.wait_for(agent_audio_detected.wait(), pause + 5)
                    response_detected = time.time()
                    perceived_latency = round((response_detected - speak_end) * 1000)
                    logger.info(
                        f"[Call {call_index}] Agent responded — "
                        f"perceived latency: {perceived_latency}ms"
                    )
                except asyncio.TimeoutError:
                    perceived_latency = None
                    logger.warning(f"[Call {call_index}] No agent response detected in {pause + 5}s")

                # Give agent time to finish speaking before next prompt
                await asyncio.sleep(max(pause - 1, 1))
            else:
                perceived_latency = None
                await asyncio.sleep(pause)

            turn_end = time.time()
            call_log.append({
                "prompt_index": i,
                "prompt_text": text,
                "speak_ms": round((speak_end - turn_start) * 1000),
                "perceived_latency_ms": perceived_latency,
                "total_turn_ms": round((turn_end - turn_start) * 1000),
            })

        # Brief pause then disconnect
        await asyncio.sleep(2)

    except Exception as e:
        logger.error(f"[Call {call_index}] Error during call: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if _audio_monitor_task[0] and not _audio_monitor_task[0].done():
            _audio_monitor_task[0].cancel()
        await room.disconnect()
        if http_session:
            await http_session.close()

    return {
        "call_index": call_index,
        "turns": call_log,
        "prompt_count": len(prompts),
    }


def _extract_session_metrics(session_data: dict) -> dict:
    """Extract key metrics from phone agent session log."""
    events = session_data.get("events", [])
    metrics = {}

    # LLM TTFT
    llm_events = [e for e in events if e.get("kind") == "llm" and e.get("ttft_ms")]
    if llm_events:
        ttfts = [e["ttft_ms"] for e in llm_events]
        metrics["llm_ttft_values"] = ttfts
        metrics["llm_ttft_avg"] = round(sum(ttfts) / len(ttfts))
        metrics["llm_ttft_p90"] = round(sorted(ttfts)[int(len(ttfts) * 0.9)])

    # TTS TTFB
    tts_events = [e for e in events if e.get("kind") == "tts" and e.get("ttfb_ms")]
    if tts_events:
        ttfbs = [e["ttfb_ms"] for e in tts_events]
        metrics["tts_ttfb_avg"] = round(sum(ttfbs) / len(ttfbs))

    # EOU
    eou_events = [e for e in events if e.get("kind") == "eou"]
    if eou_events:
        delays = [e["utterance_delay_ms"] for e in eou_events if e.get("utterance_delay_ms")]
        if delays:
            metrics["eou_delay_avg"] = round(sum(delays) / len(delays))

    # Ensemble
    ens_events = [e for e in events if e.get("kind") == "ensemble_validation"]
    if ens_events:
        confs = [e["word_confidence"] for e in ens_events]
        metrics["ensemble_conf_avg"] = round(sum(confs) / len(confs), 3)

    # STT turns
    stt_events = [e for e in events if e.get("kind") == "stt_turn"]
    metrics["stt_turn_count"] = len(stt_events)

    metrics["duration_sec"] = session_data.get("durationSec", 0)
    metrics["event_count"] = len(events)
    metrics["raw_log_count"] = len(session_data.get("rawLogs", []))

    return metrics


async def run_test(scenario_path: str, num_calls: int = 1, run_id: str | None = None):
    """Run multiple test calls and aggregate results."""
    with open(scenario_path) as f:
        scenario = yaml.safe_load(f)

    run_id = run_id or f"{scenario['name']}_{int(time.time())}"
    run_dir = Path("logs/runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"{'=' * 60}")
    logger.info(f"  Test Run: {run_id}")
    logger.info(f"  Scenario: {scenario['name']} ({scenario.get('language', 'en')})")
    logger.info(f"  Calls: {num_calls}")
    logger.info(f"{'=' * 60}")

    all_results = []

    for i in range(num_calls):
        logger.info(f"\n--- Call {i+1}/{num_calls} ---")
        result = await run_single_call(
            scenario=scenario,
            run_dir=run_dir,
            call_index=i + 1,
            agent_id=DEFAULT_AGENT_ID,
            campaign_phone=DEFAULT_CAMPAIGN_PHONE,
        )
        all_results.append(result)

        # Brief pause between calls
        if i < num_calls - 1:
            logger.info("Pausing 5s between calls...")
            await asyncio.sleep(5)

    # Aggregate metrics across all calls
    aggregate = _aggregate_results(all_results)
    aggregate["run_id"] = run_id
    aggregate["scenario"] = scenario["name"]
    aggregate["num_calls"] = num_calls

    # Save
    (run_dir / "aggregate.json").write_text(json.dumps(aggregate, indent=2, default=str))

    # Print report
    _print_report(aggregate)

    logger.info(f"\nResults saved to: {run_dir}/")
    return aggregate


def _aggregate_results(results: list[dict]) -> dict:
    """Aggregate metrics across multiple calls."""
    all_ttfts = []
    all_tts_ttfbs = []
    all_eou_delays = []
    all_ensemble_confs = []
    successful = 0

    for r in results:
        if "error" in r:
            continue
        successful += 1
        sm = r.get("session_metrics", {})
        all_ttfts.extend(sm.get("llm_ttft_values", []))
        if sm.get("tts_ttfb_avg"):
            all_tts_ttfbs.append(sm["tts_ttfb_avg"])
        if sm.get("eou_delay_avg"):
            all_eou_delays.append(sm["eou_delay_avg"])
        if sm.get("ensemble_conf_avg"):
            all_ensemble_confs.append(sm["ensemble_conf_avg"])

    agg = {
        "successful_calls": successful,
        "failed_calls": len(results) - successful,
    }

    if all_ttfts:
        agg["llm_ttft_avg_ms"] = round(sum(all_ttfts) / len(all_ttfts))
        agg["llm_ttft_p50_ms"] = round(sorted(all_ttfts)[len(all_ttfts) // 2])
        agg["llm_ttft_p90_ms"] = round(sorted(all_ttfts)[int(len(all_ttfts) * 0.9)])
        agg["llm_ttft_min_ms"] = min(all_ttfts)
        agg["llm_ttft_max_ms"] = max(all_ttfts)
        agg["llm_ttft_samples"] = len(all_ttfts)

    if all_tts_ttfbs:
        agg["tts_ttfb_avg_ms"] = round(sum(all_tts_ttfbs) / len(all_tts_ttfbs))

    if all_eou_delays:
        agg["eou_delay_avg_ms"] = round(sum(all_eou_delays) / len(all_eou_delays))

    if all_ensemble_confs:
        agg["ensemble_conf_avg"] = round(sum(all_ensemble_confs) / len(all_ensemble_confs), 3)

    return agg


def _print_report(agg: dict):
    """Print human-readable aggregate report."""
    print()
    print(f"{'=' * 60}")
    print(f"  RESULTS: {agg.get('scenario', '?')} ({agg['successful_calls']}/{agg['num_calls']} calls)")
    print(f"{'=' * 60}")

    if "llm_ttft_avg_ms" in agg:
        print(f"  LLM Time-to-First-Token:")
        print(f"    avg:  {agg['llm_ttft_avg_ms']}ms")
        print(f"    p50:  {agg['llm_ttft_p50_ms']}ms")
        print(f"    p90:  {agg['llm_ttft_p90_ms']}ms")
        print(f"    min:  {agg['llm_ttft_min_ms']}ms  max: {agg['llm_ttft_max_ms']}ms")
        print(f"    samples: {agg['llm_ttft_samples']}")

    if "tts_ttfb_avg_ms" in agg:
        print(f"  TTS Time-to-First-Byte:  {agg['tts_ttfb_avg_ms']}ms avg")

    if "eou_delay_avg_ms" in agg:
        print(f"  EOU Delay:               {agg['eou_delay_avg_ms']}ms avg")

    if "ensemble_conf_avg" in agg:
        print(f"  Ensemble Confidence:     {agg['ensemble_conf_avg']:.1%} avg")

    if agg.get("failed_calls", 0) > 0:
        print(f"  \033[91mFailed calls: {agg['failed_calls']}\033[0m")

    print(f"{'=' * 60}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Run voice agent test")
    parser.add_argument("scenario", help="Path to scenario YAML file")
    parser.add_argument("--calls", type=int, default=2, help="Number of calls (default: 2)")
    parser.add_argument("--run-id", help="Custom run ID")
    args = parser.parse_args()

    if not Path(args.scenario).exists():
        print(f"Scenario not found: {args.scenario}")
        sys.exit(1)

    asyncio.run(run_test(args.scenario, num_calls=args.calls, run_id=args.run_id))


if __name__ == "__main__":
    main()
