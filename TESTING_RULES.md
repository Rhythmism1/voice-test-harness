# Testing Rules

## Prerequisites
1. Phone agent MUST be running in dev mode in a separate terminal: `cd ../phone && uv run src/main.py dev`
2. The phone agent registers as a LiveKit worker and waits for dispatches. The test harness dispatches jobs to it.
3. The phone agent MUST have session logging enabled (dev mode does this automatically).

## How a Test Run Works
1. Orchestrator creates a LiveKit room
2. Orchestrator dispatches the phone agent to that room with test metadata
3. Orchestrator joins the room as a participant (the "caller")
4. Orchestrator speaks prompts via TTS, waits for agent responses
5. After all prompts, orchestrator disconnects
6. Phone agent writes session log to `../phone/logs/sessions/`
7. Orchestrator copies the session log and extracts metrics
8. Results saved to `logs/runs/<run_id>/`

## Test Parameters
- **Calls per run**: Default 2. Use `--calls N` to change.
- **Prompts per call**: Defined in scenario YAML. Aim for 5-6 per call for TTFT measurement.
- **Pause between prompts**: Default 3s + 2s for response. Configurable per prompt.
- **Pause between calls**: 5s to let the phone agent reset.

## What Gets Measured
- **LLM TTFT** (Time-to-First-Token): How fast the LLM starts generating a response after the user finishes speaking. Measured from the phone agent's session log.
- **TTS TTFB** (Time-to-First-Byte): How fast TTS starts producing audio after receiving LLM text.
- **EOU Delay**: How long the end-of-utterance model takes to decide the user is done speaking.
- **Ensemble Word Confidence**: Per-turn STT consensus accuracy (if ensemble is enabled).

## Output Structure
```
logs/runs/<run_id>/
├── aggregate.json          # Cross-call metrics (TTFT avg/p50/p90/min/max)
├── call_1_<ts>/
│   ├── result.json         # Per-call result with turn timing
│   └── phone_session.json  # Full phone agent session log
├── call_2_<ts>/
│   ├── result.json
│   └── phone_session.json
```

## Rules for Valid Tests
1. **No concurrent test runs.** One run at a time. The phone agent is a single worker.
2. **Wait for phone agent to be ready.** After starting `uv run src/main.py dev`, wait for the "registered agent" log before running tests.
3. **Don't modify phone agent during a run.** Code changes between runs are fine.
4. **Check for errors.** If `failed_calls > 0` in aggregate.json, the data is incomplete. Check the call result.json for error details.
5. **Minimum 2 calls per measurement.** Single-call results are noisy. Use `--calls 3` for anything you'll report.
6. **Session logs are the source of truth.** The TTFT numbers come from the phone agent's own instrumentation, not from the tester's timing.
