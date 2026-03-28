"""
Tester Agent — Acts as a caller in end-to-end phone tests.

This is a real LiveKit voice agent that joins a room and has a conversation
with the phone agent. It uses LLM to generate natural responses based on
a persona defined in the scenario.

Started as a worker via `uv run python agent.py dev`. The orchestrator
dispatches it to the test room with scenario metadata.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
)
from livekit.plugins import cartesia, deepgram, openai, silero

# Load env: e2e config first (LiveKit creds), then provider keys from phone agent
HARNESS_DIR = Path(__file__).parent.parent
load_dotenv(str(Path(__file__).parent / ".env.e2e"))  # LiveKit inbound project
load_dotenv(str(HARNESS_DIR / ".env.local"))           # Twilio creds etc
load_dotenv(str(HARNESS_DIR.parent / "phone" / ".env.local"))  # Provider keys (Cartesia, OpenAI, etc)

logger = logging.getLogger("tester-agent")


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext):
    """Tester agent entrypoint."""
    metadata = json.loads(ctx.job.metadata) if ctx.job.metadata else {}
    scenario_path = metadata.get("scenario_path")
    run_id = metadata.get("run_id", f"run_{int(time.time())}")
    call_index = metadata.get("call_index", 1)

    # Load scenario — from metadata, or from the "active" scenario file
    active_scenario = HARNESS_DIR / "active_scenario.yaml"
    scenario = None

    if scenario_path and Path(scenario_path).exists():
        with open(scenario_path) as f:
            scenario = yaml.safe_load(f)
    elif active_scenario.exists():
        with open(active_scenario) as f:
            scenario = yaml.safe_load(f)
        logger.info(f"[Tester] Using active scenario: {active_scenario}")
    else:
        # Default: generic test caller
        scenario = {
            "name": "default",
            "language": "en",
            "tester": {
                "instructions": "You are a test caller. Have a natural, friendly conversation. Ask about services available.",
            },
            "prompts": [],
        }
        logger.info("[Tester] No scenario found — using default instructions")

    tester_config = scenario.get("tester", {})
    instructions = tester_config.get("instructions", "You are a test caller. Have a natural conversation.")
    voice = tester_config.get("voice", "a0e99841-438c-4a64-b679-ae501e7d6091")
    language = scenario.get("language", "en")

    logger.info(f"[Tester] Scenario: {scenario['name']}, Run: {run_id}")
    logger.info(f"[Tester] Instructions: {instructions[:80]}...")

    await ctx.connect()
    logger.info(f"[Tester] Connected to room: {ctx.room.name}")

    # Track conversation
    heard_texts = []
    said_texts = []
    call_start = time.time()

    import aiohttp
    http_session = aiohttp.ClientSession()

    tts = cartesia.TTS(voice=voice, http_session=http_session)
    stt = deepgram.STT(model="nova-3", language=language)
    llm = openai.LLM(model="gpt-4o-mini")  # Cheaper/faster model for the tester

    session = AgentSession(
        stt=stt,
        tts=tts,
        llm=llm,
        vad=ctx.proc.userdata["vad"],
    )

    @session.on("user_input_transcribed")
    def on_heard(event):
        if event.is_final and event.transcript.strip():
            heard_texts.append({
                "text": event.transcript,
                "elapsed": round(time.time() - call_start, 1),
            })
            logger.info(f"[Tester] Heard agent say: {event.transcript[:60]}")

    # Create the tester agent with the persona
    agent = Agent(instructions=instructions)

    await session.start(agent=agent, room=ctx.room)
    logger.info("[Tester] Session started, listening to agent...")

    # Let the conversation run for the expected duration
    # Calculate total time from prompts
    prompts = scenario.get("prompts", [])
    total_pause = sum(p.get("pause_after_sec", 5) + 3 for p in prompts)
    wait_time = max(total_pause, 30)  # At least 30s

    logger.info(f"[Tester] Conversation will run for ~{wait_time}s")
    await asyncio.sleep(wait_time)

    # Save results
    log_dir = HARNESS_DIR / "logs" / "runs" / run_id
    log_dir.mkdir(parents=True, exist_ok=True)

    results = {
        "run_id": run_id,
        "call_index": call_index,
        "scenario": scenario["name"],
        "tester_instructions": instructions[:200],
        "heard_from_agent": heard_texts,
        "duration_sec": round(time.time() - call_start),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
    }

    tester_log = log_dir / f"call_{call_index}_tester.json"
    tester_log.write_text(json.dumps(results, indent=2))
    logger.info(f"[Tester] Results saved to {tester_log}")

    await http_session.close()
    logger.info("[Tester] Done")


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="voice-tester",
        )
    )
