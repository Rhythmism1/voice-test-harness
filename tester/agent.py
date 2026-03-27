"""
Test Agent — Joins a LiveKit room and speaks scripted prompts.

This is the "caller" side of the test. It:
1. Speaks each prompt via TTS
2. Waits for the phone agent to respond
3. Records what the phone agent said via STT
4. Logs everything for post-run analysis
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    AgentSession,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
)
from livekit.agents.stt import STT, SpeechEventType
from livekit.plugins import cartesia, deepgram, silero

load_dotenv(".env.local")
load_dotenv(".env")

logger = logging.getLogger("test-agent")

# Global state passed via process userdata
_scenario = None
_run_id = None


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext):
    """Test agent entrypoint — speaks prompts, records responses."""
    global _scenario, _run_id

    # Parse scenario from metadata
    metadata = json.loads(ctx.job.metadata) if ctx.job.metadata else {}
    scenario_path = metadata.get("scenario_path")
    run_id = metadata.get("run_id", f"run_{int(time.time())}")
    _run_id = run_id

    if not scenario_path or not Path(scenario_path).exists():
        logger.error(f"Scenario not found: {scenario_path}")
        return

    with open(scenario_path) as f:
        scenario = yaml.safe_load(f)
    _scenario = scenario

    logger.info(f"[TestAgent] Scenario: {scenario['name']}, Run: {run_id}")

    # Connect to room
    await ctx.connect()
    logger.info(f"[TestAgent] Connected to room: {ctx.room.name}")

    # Wait for the phone agent to join
    participant = await _wait_for_other_participant(ctx.room, timeout=30)
    if not participant:
        logger.error("[TestAgent] Phone agent never joined")
        return

    logger.info(f"[TestAgent] Phone agent joined: {participant.identity}")

    # Set up STT to record what we hear
    tester_config = scenario.get("tester", {})
    stt = deepgram.STT(model="nova-3", language=scenario.get("language", "en"))
    tts = cartesia.TTS(
        voice=tester_config.get("voice", "a0e99841-438c-4a64-b679-ae501e7d6091"),
    )

    # Run the scripted conversation
    results = await _run_script(ctx, scenario, stt, tts, participant)

    # Save results
    _save_results(run_id, scenario, results)
    logger.info(f"[TestAgent] Done. {len(results['turns'])} turns completed.")

    # Disconnect
    await ctx.room.disconnect()


async def _wait_for_other_participant(room: rtc.Room, timeout: float = 30) -> rtc.RemoteParticipant | None:
    """Wait for another participant (the phone agent) to join."""
    # Check if already present
    for p in room.remote_participants.values():
        return p

    # Wait for join event
    fut = asyncio.Future()

    @room.on("participant_connected")
    def on_join(participant: rtc.RemoteParticipant):
        if not fut.done():
            fut.set_result(participant)

    try:
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        return None


async def _run_script(ctx: JobContext, scenario: dict, stt: STT, tts, participant) -> dict:
    """Execute the scripted prompts and record responses."""
    prompts = scenario.get("prompts", [])
    turns = []
    heard_texts = []

    # Subscribe to participant's audio
    audio_stream = None
    for pub in participant.track_publications.values():
        if pub.kind == rtc.TrackKind.KIND_AUDIO and pub.track:
            audio_stream = rtc.AudioStream(pub.track)
            break

    if not audio_stream:
        # Wait for track to be published
        track_fut = asyncio.Future()

        @ctx.room.on("track_subscribed")
        def on_track(track, publication, p):
            if p.identity == participant.identity and track.kind == rtc.TrackKind.KIND_AUDIO:
                if not track_fut.done():
                    track_fut.set_result(track)

        try:
            track = await asyncio.wait_for(track_fut, 10)
            audio_stream = rtc.AudioStream(track)
        except asyncio.TimeoutError:
            logger.warning("[TestAgent] No audio track from phone agent")

    # Create audio source for our TTS output
    audio_source = rtc.AudioSource(sample_rate=24000, num_channels=1)
    track = rtc.LocalAudioTrack.create_audio_track("tester-audio", audio_source)
    await ctx.room.local_participant.publish_track(track)

    # Wait a moment for the greeting
    logger.info("[TestAgent] Waiting for agent greeting...")
    await asyncio.sleep(4)

    for i, prompt in enumerate(prompts):
        turn_start = time.time()
        text = prompt["text"]
        logger.info(f"[TestAgent] Speaking prompt {i+1}/{len(prompts)}: {text[:60]}...")

        # Synthesize and send audio
        synth = tts.synthesize(text)
        async for audio_event in synth:
            await audio_source.capture_frame(audio_event.frame)

        speak_end = time.time()

        # Wait for response
        pause = prompt.get("pause_after_sec", 2.0)
        response_text = ""

        if prompt.get("wait_for_response", True) and audio_stream:
            # Listen for agent response via STT
            response_text = await _listen_for_response(stt, audio_stream, timeout=15)

        turn_end = time.time()

        turn = {
            "prompt_index": i,
            "prompt_text": text,
            "speak_duration_ms": round((speak_end - turn_start) * 1000),
            "response_text": response_text,
            "response_wait_ms": round((turn_end - speak_end) * 1000),
            "total_turn_ms": round((turn_end - turn_start) * 1000),
        }
        turns.append(turn)
        heard_texts.append(response_text)
        logger.info(f"[TestAgent] Heard: {response_text[:80]}...")

        # Pause before next prompt
        await asyncio.sleep(pause)

    return {
        "turns": turns,
        "all_heard": " ".join(heard_texts),
        "scenario": scenario["name"],
    }


async def _listen_for_response(stt: STT, audio_stream, timeout: float = 15) -> str:
    """Listen to audio stream via STT until silence, return accumulated text."""
    stream = stt.stream()
    texts = []
    last_final = time.time()

    async def feed_audio():
        async for frame_event in audio_stream:
            stream.push_frame(frame_event.frame)

    async def collect_transcripts():
        nonlocal last_final
        async for event in stream:
            if event.type == SpeechEventType.FINAL_TRANSCRIPT and event.alternatives:
                text = event.alternatives[0].text
                if text.strip():
                    texts.append(text)
                    last_final = time.time()

    feed_task = asyncio.create_task(feed_audio())
    collect_task = asyncio.create_task(collect_transcripts())

    # Wait until we get silence (no new finals for 3s) or timeout
    start = time.time()
    while time.time() - start < timeout:
        await asyncio.sleep(0.5)
        if texts and time.time() - last_final > 3.0:
            break

    feed_task.cancel()
    collect_task.cancel()
    await stream.aclose()

    return " ".join(texts)


def _save_results(run_id: str, scenario: dict, results: dict):
    """Save test results to logs/runs/<run_id>/tester.json"""
    log_dir = Path("logs/runs") / run_id
    log_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "runId": run_id,
        "scenario": scenario["name"],
        "language": scenario.get("language", "en"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "turns": results["turns"],
        "allHeard": results["all_heard"],
        "thresholds": scenario.get("thresholds", {}),
    }

    path = log_dir / "tester.json"
    path.write_text(json.dumps(payload, indent=2))
    logger.info(f"[TestAgent] Results saved to {path}")


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="voice-tester",
        )
    )
