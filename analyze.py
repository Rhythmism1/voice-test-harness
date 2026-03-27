"""
Post-Run Analysis

Reads logs from both agents and computes metrics.

Usage:
    uv run python analyze.py logs/runs/<run_id>/
    uv run python analyze.py logs/runs/<run_id>/ --json  # Machine-readable output
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from jiwer import wer as compute_wer
except ImportError:
    compute_wer = None


def analyze_run(run_dir: str, output_json: bool = False) -> dict:
    """Analyze a completed test run and return metrics."""
    run_path = Path(run_dir)

    tester_path = run_path / "tester.json"
    phone_path = run_path / "phone_session.json"

    results = {
        "run_dir": str(run_path),
        "tester_found": tester_path.exists(),
        "phone_found": phone_path.exists(),
        "metrics": {},
        "pass": True,
        "failures": [],
    }

    # Load tester data
    tester = None
    if tester_path.exists():
        tester = json.loads(tester_path.read_text())

    # Load phone session data
    phone = None
    if phone_path.exists():
        phone = json.loads(phone_path.read_text())

    # === Tester Metrics ===
    if tester:
        turns = tester.get("turns", [])
        results["metrics"]["turn_count"] = len(turns)
        results["metrics"]["scenario"] = tester.get("scenario", "unknown")
        results["metrics"]["language"] = tester.get("language", "en")

        # Turn timing
        if turns:
            response_waits = [t["response_wait_ms"] for t in turns if t.get("response_wait_ms")]
            if response_waits:
                results["metrics"]["avg_response_wait_ms"] = round(sum(response_waits) / len(response_waits))
                results["metrics"]["p90_response_wait_ms"] = round(sorted(response_waits)[int(len(response_waits) * 0.9)])

        # WER calculation
        if compute_wer and turns:
            all_reference = " ".join(t["prompt_text"] for t in turns)
            all_heard = tester.get("allHeard", "")
            if all_heard.strip():
                wer_score = compute_wer(all_reference.lower(), all_heard.lower())
                results["metrics"]["wer"] = round(wer_score, 4)

        # Check thresholds
        thresholds = tester.get("thresholds", {})
        if "max_wer" in thresholds and "wer" in results["metrics"]:
            if results["metrics"]["wer"] > thresholds["max_wer"]:
                results["pass"] = False
                results["failures"].append(
                    f"WER {results['metrics']['wer']:.1%} exceeds threshold {thresholds['max_wer']:.1%}"
                )

    # === Phone Agent Metrics ===
    if phone:
        events = phone.get("events", [])

        # LLM latency
        llm_events = [e for e in events if e.get("kind") == "llm" and e.get("ttft_ms")]
        if llm_events:
            ttfts = [e["ttft_ms"] for e in llm_events]
            results["metrics"]["avg_llm_ttft_ms"] = round(sum(ttfts) / len(ttfts))
            results["metrics"]["p90_llm_ttft_ms"] = round(sorted(ttfts)[int(len(ttfts) * 0.9)])

        # TTS latency
        tts_events = [e for e in events if e.get("kind") == "tts" and e.get("ttfb_ms")]
        if tts_events:
            ttfbs = [e["ttfb_ms"] for e in tts_events]
            results["metrics"]["avg_tts_ttfb_ms"] = round(sum(ttfbs) / len(ttfbs))

        # EOU latency
        eou_events = [e for e in events if e.get("kind") == "eou" and e.get("utterance_delay_ms")]
        if eou_events:
            delays = [e["utterance_delay_ms"] for e in eou_events]
            results["metrics"]["avg_eou_delay_ms"] = round(sum(delays) / len(delays))

        # Ensemble validation
        ensemble_events = [e for e in events if e.get("kind") == "ensemble_validation"]
        if ensemble_events:
            confidences = [e["word_confidence"] for e in ensemble_events]
            results["metrics"]["avg_ensemble_confidence"] = round(sum(confidences) / len(confidences), 3)
            results["metrics"]["min_ensemble_confidence"] = round(min(confidences), 3)
            results["metrics"]["ensemble_turns"] = len(ensemble_events)

        # STT turns
        stt_events = [e for e in events if e.get("kind") == "stt_turn"]
        if stt_events:
            results["metrics"]["stt_turn_count"] = len(stt_events)
            results["metrics"]["phone_heard"] = " ".join(
                e.get("transcript", "") for e in stt_events
            )[:500]

        # Call duration
        results["metrics"]["call_duration_sec"] = phone.get("durationSec", 0)

        # Raw log count
        raw_logs = phone.get("rawLogs", [])
        results["metrics"]["raw_log_count"] = len(raw_logs)

        # Check thresholds from tester
        if tester:
            thresholds = tester.get("thresholds", {})
            if "max_avg_llm_ttft_ms" in thresholds and "avg_llm_ttft_ms" in results["metrics"]:
                if results["metrics"]["avg_llm_ttft_ms"] > thresholds["max_avg_llm_ttft_ms"]:
                    results["pass"] = False
                    results["failures"].append(
                        f"LLM TTFT {results['metrics']['avg_llm_ttft_ms']}ms exceeds {thresholds['max_avg_llm_ttft_ms']}ms"
                    )
            if "min_ensemble_word_confidence" in thresholds and "avg_ensemble_confidence" in results["metrics"]:
                if results["metrics"]["avg_ensemble_confidence"] < thresholds["min_ensemble_word_confidence"]:
                    results["pass"] = False
                    results["failures"].append(
                        f"Ensemble confidence {results['metrics']['avg_ensemble_confidence']:.1%} below {thresholds['min_ensemble_word_confidence']:.1%}"
                    )

    # === Phone Stdout Logs (full terminal output) ===
    stdout_path = run_path / "phone_stdout.log"
    if stdout_path.exists():
        stdout_metrics = _parse_stdout_log(stdout_path)
        results["metrics"].update(stdout_metrics)
        results["stdout_log_lines"] = sum(1 for _ in open(stdout_path))

    # === Output ===
    if output_json:
        print(json.dumps(results, indent=2))
    else:
        _print_report(results)

    # Save analysis
    analysis_path = run_path / "analysis.json"
    analysis_path.write_text(json.dumps(results, indent=2))

    return results


def _parse_stdout_log(path: Path) -> dict:
    """Parse the full terminal output for metrics not in the session log.

    The phone agent's stdout contains LiveKit framework logs with:
    - EOU predictions (eou_probability, duration, input text)
    - Memory usage warnings (memory_usage_mb)
    - Preemptive generation timing
    - Speechmatics Start/EndOfTurn events
    """
    import re

    metrics: dict = {}
    eou_probs = []
    memory_readings = []
    speechmatics_events = {"StartOfTurn": 0, "EndOfTurn": 0, "EndOfTranscript": 0}

    text = path.read_text(errors="replace")
    for line in text.split("\n"):
        # EOU predictions
        m = re.search(r'"eou_probability":\s*([\d.e-]+)', line)
        if m:
            eou_probs.append(float(m.group(1)))

        # Memory usage
        m = re.search(r'"memory_usage_mb":\s*([\d.]+)', line)
        if m:
            memory_readings.append(float(m.group(1)))

        # Speechmatics events
        for event_type in speechmatics_events:
            if event_type in line and "echmatics" in line:
                speechmatics_events[event_type] += 1

    if eou_probs:
        metrics["eou_prediction_count"] = len(eou_probs)
        metrics["eou_max_probability"] = round(max(eou_probs), 4)
        # How many predictions were above commit threshold (typically ~0.5)
        metrics["eou_above_threshold"] = sum(1 for p in eou_probs if p > 0.5)

    if memory_readings:
        metrics["peak_memory_mb"] = round(max(memory_readings), 1)
        metrics["avg_memory_mb"] = round(sum(memory_readings) / len(memory_readings), 1)

    if any(v > 0 for v in speechmatics_events.values()):
        metrics["speechmatics_start_of_turn"] = speechmatics_events["StartOfTurn"]
        metrics["speechmatics_end_of_turn"] = speechmatics_events["EndOfTurn"]
        metrics["speechmatics_end_of_transcript"] = speechmatics_events["EndOfTranscript"]

    return metrics


def _print_report(results: dict):
    """Print human-readable analysis report."""
    m = results["metrics"]
    print()
    print(f"{'=' * 60}")
    print(f"  Test Run Analysis: {m.get('scenario', 'unknown')}")
    print(f"  Language: {m.get('language', 'en')}  |  Duration: {m.get('call_duration_sec', '?')}s")
    print(f"{'=' * 60}")

    if "wer" in m:
        print(f"  WER:                    {m['wer']:.1%}")
    if "avg_llm_ttft_ms" in m:
        print(f"  LLM TTFT (avg):         {m['avg_llm_ttft_ms']}ms")
    if "p90_llm_ttft_ms" in m:
        print(f"  LLM TTFT (p90):         {m['p90_llm_ttft_ms']}ms")
    if "avg_tts_ttfb_ms" in m:
        print(f"  TTS TTFB (avg):         {m['avg_tts_ttfb_ms']}ms")
    if "avg_eou_delay_ms" in m:
        print(f"  EOU Delay (avg):        {m['avg_eou_delay_ms']}ms")
    if "avg_ensemble_confidence" in m:
        print(f"  Ensemble Conf (avg):    {m['avg_ensemble_confidence']:.1%}")
        print(f"  Ensemble Conf (min):    {m['min_ensemble_confidence']:.1%}")
    if "turn_count" in m:
        print(f"  Turns:                  {m['turn_count']}")
    if "peak_memory_mb" in m:
        print(f"  Peak Memory:            {m['peak_memory_mb']}MB")
    if "eou_prediction_count" in m:
        print(f"  EOU Predictions:        {m['eou_prediction_count']} ({m.get('eou_above_threshold', 0)} above threshold)")
    if "speechmatics_start_of_turn" in m:
        print(f"  Speechmatics Turns:     {m['speechmatics_start_of_turn']} start / {m['speechmatics_end_of_turn']} end")

    print(f"{'─' * 60}")
    status = "PASS" if results["pass"] else "FAIL"
    color = "\033[92m" if results["pass"] else "\033[91m"
    print(f"  Result: {color}{status}\033[0m")
    for f in results.get("failures", []):
        print(f"    - {f}")
    print()

    if "phone_heard" in m:
        print(f"  Phone agent heard:")
        print(f"    {m['phone_heard'][:200]}...")
    print()


def main():
    parser = argparse.ArgumentParser(description="Analyze a test run")
    parser.add_argument("run_dir", help="Path to run directory (logs/runs/<run_id>/)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if not Path(args.run_dir).exists():
        print(f"Run directory not found: {args.run_dir}")
        sys.exit(1)

    results = analyze_run(args.run_dir, output_json=args.json)
    sys.exit(0 if results["pass"] else 1)


if __name__ == "__main__":
    main()
