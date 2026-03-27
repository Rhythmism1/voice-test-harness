# Claude Code Instructions — Test Harness

This is an autonomous voice agent testing framework. You are the primary operator.

## How to use this

When given a task that requires empirical testing (STT accuracy, latency measurement, prompt changes), use this harness:

1. **Define or pick a scenario** from `scenarios/`. Create a new YAML if needed.
2. **Run it**: `uv run python run.py scenarios/<name>.yaml`
3. **Read results**: `cat logs/runs/<run_id>/analysis.json`
4. **If test fails**: read the phone session log at `logs/runs/<run_id>/phone_session.json` for raw events and logs
5. **Make code changes** to `../phone/` based on what the logs show
6. **Re-run** the same scenario to verify the fix
7. **Report** the before/after metrics to the user

## Key paths

- Phone agent source: `../phone/`
- Phone agent session logs: `../phone/logs/sessions/` (written in dev mode only)
- Test scenarios: `./scenarios/`
- Run results: `./logs/runs/<run_id>/`
- Harness config: `./harness.yaml`

## Log files per run

Each run produces these files in `logs/runs/<run_id>/`:

| File | What | How to use |
|------|------|-----------|
| `phone_stdout.log` | **Full terminal output** — EOU predictions with probabilities, memory warnings, Speechmatics WebSocket events, LiveKit debug logs. This is the richest data source. | Parse with regex for specific metrics. `analyze.py` already extracts EOU probs, memory, Speechmatics event counts. |
| `phone_session.json` | **Structured events** — STT turns, LLM/TTS/EOU metrics, ensemble validations, Python raw logs. Written by SessionLogger in dev mode. | Read `events[]` for typed data, `rawLogs[]` for Python logger output. |
| `tester.json` | **Tester side** — what prompts were spoken, what was heard back, timing per turn. | Compare `prompt_text` vs phone agent's STT output for WER. |
| `analysis.json` | **Computed metrics** — WER, avg latencies, ensemble confidence, memory, pass/fail. | Machine-readable summary. Read this first. |
| `phone_stdout.log` has data that `phone_session.json` does NOT — specifically LiveKit's Rust/Go layer output (EOU predictions, memory usage, connection events) which bypasses Python's logging system.

## When writing new scenarios

```yaml
name: descriptive_name
language: en  # or tr, ar, etc.
prompts:
  - text: "What the test agent says"
    pause_after_sec: 2.0
    wait_for_response: true
thresholds:
  max_wer: 0.15
  max_avg_llm_ttft_ms: 1500
```

## Important

- The phone agent MUST be started with `dev` mode for session logs to be written
- The harness starts the phone agent itself — don't start it separately
- LiveKit credentials come from `../phone/.env.local`
- Each run creates a room, runs the test, deletes the room — no manual cleanup needed
