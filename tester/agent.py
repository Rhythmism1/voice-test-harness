"""
Tester Agent — A LiveKit agent that acts as a caller.

Joins a room, listens to the phone agent's greeting, speaks scripted
prompts, records what it hears back. Runs as a real LiveKit agent worker
so it handles audio natively (no TwiML, no Twilio TTS).

Started by the orchestrator via LiveKit dispatch. Receives scenario
config in job metadata.
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
    Agent,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    RoomInputOptions,
)
from livekit.agents.voice.events import AgentState
from livekit.plugins import cartesia, deepgram, silero

load_dotenv(str(Path(__file__).parent.parent / ".env.local"))
load_dotenv(str(Path(__file__).parent.parent.parent / "phone" / ".env.local"))

logger = logging.getLogger("tester-agent")


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext):
    """Tester agent — speaks prompts, records responses."""
    metadata = json.loads(ctx.job.metadata) if ctx.job.metadata else {}
    scenario_path = metadata.get("scenario_path")
    run_id = metadata.get("run_id", f"run_{int(time.time())}")
    call_index = metadata.get("call_index", 1)

    if not scenario_path or not Path(scenario_path).exists():
        logger.error(f"Scenario not found: {scenario_path}")
        return

    with open(scenario_path) as f:
        scenario = yaml.safe_load(f)

    logger.info(f"[Tester] Scenario: {scenario['name']}, Run: {run_id}, Call: {call_index}")

    await ctx.connect()
    logger.info(f"[Tester] Connected to room: {ctx.room.name}")

    # Wait for the phone agent to join
    agent_participant = None
    for p in ctx.room.remote_participants.values():
        agent_participant = p
        break

    if not agent_participant:
        join_fut = asyncio.Future()

        @ctx.room.on("participant_connected")
        def on_join(p):
            if not join_fut.done():
                join_fut.set_result(p)

        try:
            agent_participant = await asyncio.wait_for(join_fut, 20)
        except asyncio.TimeoutError:
            logger.error("[Tester] Phone agent never joined")
            return

    logger.info(f"[Tester] Phone agent joined: {agent_participant.identity}")

    # Create a simple agent that speaks the prompts
    tester = TesterVoiceAgent(scenario=scenario, run_id=run_id, call_index=call_index)

    import aiohttp
    http_session = aiohttp.ClientSession()

    tts_voice = scenario.get("tester", {}).get("voice", "a0e99841-438c-4a64-b679-ae501e7d6091")
    tts = cartesia.TTS(voice=tts_voice, http_session=http_session)
    stt = deepgram.STT(model="nova-3", language=scenario.get("language", "en"))

    session = AgentSession(
        stt=stt,
        tts=tts,
        vad=ctx.proc.userdata["vad"],
    )

    # Track what we hear from the phone agent
    heard_texts = []
    turn_log = []

    @session.on("user_input_transcribed")
    def on_user_input(event):
        if event.is_final and event.transcript.strip():
            heard_texts.append(event.transcript)
            logger.info(f"[Tester] Heard: {event.transcript[:60]}")

    await session.start(
        agent=tester,
        room=ctx.room,
    )

    # Wait for the agent to finish its script
    await tester.done_event.wait()

    # Brief pause then save results
    await asyncio.sleep(2)

    # Save results
    results = {
        "run_id": run_id,
        "call_index": call_index,
        "scenario": scenario["name"],
        "heard_from_agent": heard_texts,
        "all_heard": " ".join(heard_texts),
        "turns": tester.turn_log,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
    }

    log_dir = Path("logs/runs") / run_id / f"call_{call_index}_{int(time.time())}"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "tester.json").write_text(json.dumps(results, indent=2))
    logger.info(f"[Tester] Results saved to {log_dir / 'tester.json'}")

    await http_session.close()


class TesterVoiceAgent(Agent):
    """Agent that speaks scripted prompts with pauses between them."""

    def __init__(self, scenario: dict, run_id: str, call_index: int):
        super().__init__(
            instructions="You are a test caller. Just speak the scripted prompts.",
        )
        self._scenario = scenario
        self._run_id = run_id
        self._call_index = call_index
        self.done_event = asyncio.Event()
        self.turn_log = []

    async def on_enter(self):
        """Called when agent becomes active. Wait for greeting then speak prompts."""
        prompts = self._scenario.get("prompts", [])

        # Wait for phone agent's greeting
        wait = self._scenario.get("tester", {}).get("wait_after_greeting_sec", 4)
        logger.info(f"[Tester] Waiting {wait}s for agent greeting...")
        await asyncio.sleep(wait)

        for i, prompt in enumerate(prompts):
            text = prompt["text"]
            logger.info(f"[Tester] Speaking prompt {i+1}/{len(prompts)}: {text[:50]}...")

            turn_start = time.time()
            self.session.say(text, allow_interruptions=False)

            # Wait for the phone agent to respond
            pause = prompt.get("pause_after_sec", 4)
            await asyncio.sleep(pause + 2)

            self.turn_log.append({
                "prompt_index": i,
                "prompt_text": text,
                "total_ms": round((time.time() - turn_start) * 1000),
            })

        # Done
        logger.info(f"[Tester] All {len(prompts)} prompts spoken. Finishing.")
        await asyncio.sleep(2)
        self.done_event.set()


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="voice-tester",
        )
    )
