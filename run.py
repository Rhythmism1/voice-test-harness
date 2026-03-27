"""
Test Run Orchestrator

Creates a LiveKit room, dispatches both agents (phone + tester),
waits for completion, copies session logs, and runs analysis.

Usage:
    uv run python run.py scenarios/basic_english.yaml
    uv run python run.py scenarios/basic_english.yaml --run-id my_test_1
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
from pathlib import Path

import yaml
from dotenv import load_dotenv
from livekit import api as lk_api

# Load harness config
HARNESS_DIR = Path(__file__).parent
HARNESS_CONFIG = yaml.safe_load((HARNESS_DIR / "harness.yaml").read_text())

PHONE_AGENT_DIR = (HARNESS_DIR / HARNESS_CONFIG["phone_agent"]["path"]).resolve()
PHONE_LOGS_DIR = (HARNESS_DIR / HARNESS_CONFIG["phone_agent"]["session_logs"]).resolve()
PHONE_ENV = (HARNESS_DIR / HARNESS_CONFIG["phone_agent"]["env_file"]).resolve()

# Load env from phone agent (shared LiveKit + provider creds)
load_dotenv(str(PHONE_ENV))
load_dotenv(str(HARNESS_DIR / ".env.local"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-20s %(message)s")
logger = logging.getLogger("orchestrator")


async def run_test(scenario_path: str, run_id: str | None = None) -> str | None:
    """Orchestrate a full test run. Returns run_id or None on failure."""

    with open(scenario_path) as f:
        scenario = yaml.safe_load(f)

    run_id = run_id or f"{scenario['name']}_{int(time.time())}"
    run_dir = Path("logs/runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"{'=' * 60}")
    logger.info(f"  Test Run: {run_id}")
    logger.info(f"  Scenario: {scenario['name']} ({scenario.get('language', 'en')})")
    logger.info(f"  Phone agent: {PHONE_AGENT_DIR}")
    logger.info(f"{'=' * 60}")

    # Snapshot phone session logs before test (to identify new ones after)
    pre_logs = set(PHONE_LOGS_DIR.glob("*.json")) if PHONE_LOGS_DIR.exists() else set()

    # Create LiveKit room
    lk_url = os.environ["LIVEKIT_URL"]
    lk_key = os.environ["LIVEKIT_API_KEY"]
    lk_secret = os.environ["LIVEKIT_API_SECRET"]
    room_name = f"test-{run_id}"

    lk = lk_api.LiveKitAPI(lk_url, lk_key, lk_secret)

    try:
        await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name))
        logger.info(f"Room created: {room_name}")
    except Exception as e:
        logger.error(f"Failed to create room: {e}")
        return None

    # Start phone agent
    logger.info("Starting phone agent...")
    phone_proc = subprocess.Popen(
        ["uv", "run", "src/main.py", "dev"],
        cwd=str(PHONE_AGENT_DIR),
        stdout=open(run_dir / "phone_stdout.log", "w"),
        stderr=subprocess.STDOUT,
    )

    # Give phone agent time to connect and register
    await asyncio.sleep(5)

    # Start test agent
    logger.info("Starting test agent...")
    tester_metadata = json.dumps({
        "scenario_path": str(Path(scenario_path).resolve()),
        "run_id": run_id,
    })
    tester_proc = subprocess.Popen(
        ["uv", "run", "python", "tester/agent.py", "dev",
         "--room", room_name,
         "--metadata", tester_metadata],
        cwd=str(HARNESS_DIR),
        stdout=open(run_dir / "tester_stdout.log", "w"),
        stderr=subprocess.STDOUT,
    )

    # Wait for tester to finish
    timeout = HARNESS_CONFIG["defaults"]["test_timeout_sec"]
    logger.info(f"Waiting for test completion (timeout: {timeout}s)...")

    try:
        tester_proc.wait(timeout=timeout)
        logger.info(f"Tester finished (exit code: {tester_proc.returncode})")
    except subprocess.TimeoutExpired:
        logger.warning("Test timed out, killing tester")
        tester_proc.kill()

    # Give phone agent time to finalize and write session log
    await asyncio.sleep(5)
    if phone_proc.poll() is None:
        phone_proc.terminate()
        try:
            phone_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            phone_proc.kill()

    # Clean up room
    try:
        await lk.room.delete_room(lk_api.DeleteRoomRequest(room=room_name))
        logger.info(f"Room deleted: {room_name}")
    except Exception:
        pass
    await lk.aclose()

    # Find and copy new phone session logs
    if PHONE_LOGS_DIR.exists():
        post_logs = set(PHONE_LOGS_DIR.glob("*.json"))
        new_logs = sorted(post_logs - pre_logs, key=lambda f: f.stat().st_mtime, reverse=True)
        if new_logs:
            shutil.copy2(new_logs[0], run_dir / "phone_session.json")
            logger.info(f"Copied phone session log: {new_logs[0].name}")
        else:
            logger.warning("No new phone session logs found")

    # Run analysis
    logger.info("Running analysis...")
    result = subprocess.run(
        ["uv", "run", "python", "analyze.py", str(run_dir)],
        cwd=str(HARNESS_DIR),
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout)
    if result.returncode != 0 and result.stderr:
        logger.warning(f"Analysis issues: {result.stderr[:200]}")

    logger.info(f"=== Run Complete: {run_id} ===")
    logger.info(f"Results: {run_dir}/")
    return run_id


def main():
    parser = argparse.ArgumentParser(description="Run a voice agent test scenario")
    parser.add_argument("scenario", help="Path to scenario YAML file")
    parser.add_argument("--run-id", help="Custom run ID (default: auto-generated)")
    args = parser.parse_args()

    if not Path(args.scenario).exists():
        print(f"Scenario not found: {args.scenario}")
        sys.exit(1)

    asyncio.run(run_test(args.scenario, run_id=args.run_id))


if __name__ == "__main__":
    main()
