"""Re-ask the learned-feature judge its questions with thinking on and off, and compare.

Only the judge is called; no worker runs. For every document in each task set, the judge gets
exactly what it gets at run time: the learned features from the rule-tree service and the
document's starting materials (all features are document-scope, decision K4), through the
same FeatureJudge prompt, constrained answer and greedy settings. The only difference between
the two modes is Qwen's ``enable_thinking`` chat-template switch.

Usage (on the GPU server, with the 4B judge server running):

    .venv/bin/python scripts/judge_thinking_replay.py \\
        --config experiments/configs/dev30-v4-learned3160.json \\
        --set dev30=experiments/configs/dev30-v4-learned3160.json \\
        --set learn3160=experiments/configs/learn-4b-rootonly3160-v2.json \\
        --saved dev30=experiments/results/dev30-v4-learned3160 \\
        --out experiments/results/judge-thinking-replay

--config  test config: its resolver gives the rule-tree service, artifact and judge model.
--set     NAME=CONFIG: a task set (benchmark data and task selection from that config).
--saved   NAME=RUN_DIR: a finished run of that set; its judge answers and materials hashes are
          compared with the thinking-on replay (a faithful replay reproduces them).
Answers go to OUT/calls.jsonl (resumable: finished calls are skipped), the comparison to
OUT/summary.md and OUT/summary.json.
"""

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from skill_agent.contracts import ResolveSkillRequest, source_fingerprint  # noqa: E402
from skill_agent.experiments import factory, load_config, make_resolvers, select_tasks  # noqa: E402
from skill_agent.learned_features import FeatureJudge, judge_context, judged_text  # noqa: E402
from skill_agent.models import OpenAICompatibleClient  # noqa: E402
from skill_agent.storage import read_json  # noqa: E402

MODES = ("on", "off")


class ThinkingClient(OpenAICompatibleClient):
    """The judge's client with Qwen's thinking switch set on every request."""

    def __init__(self, *, thinking, **options):
        super().__init__(**options)
        self.thinking = thinking

    async def _post(self, payload):
        payload = {**payload, "chat_template_kwargs": {"enable_thinking": self.thinking}}
        return await super()._post(payload)


def judge_client(judge_model, thinking):
    """The configured judge model as a plain client (only the 4B server needs to be up)."""
    options = {k: v for k, v in judge_model["options"].items() if k != "peers"}
    return ThinkingClient(thinking=thinking, **options)


def saved_answers(run_dir):
    """{task_id: {"values", "materials_sha256"}} from a finished run's episodes (first judge call each)."""
    answers = {}
    for path in sorted((Path(run_dir) / "episodes").glob("*.json")):
        record = read_json(path)
        for event in record.get("events", []):
            judge = ((event.get("response") or {}).get("metadata") or {}).get("feature_judge")
            if judge:
                answers[record["task_id"]] = {"values": judge["values"],
                                              "materials_sha256": judge.get("materials_sha256")}
                break
    return answers


def load_done(path):
    done = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done[(row["set"], row["task_id"], row["mode"])] = row
    return done


async def learned_features(config):
    """The rule-tree service's learned features for the test config's snapshot."""
    receipt, resolvers = make_resolvers(config)
    resolver = resolvers["reduced"]
    if not hasattr(resolver, "preflight"):
        raise ValueError("--config must use the LearnedFeatureResolver")
    tasks = select_tasks(factory(config["benchmark"]), config["run"])
    await resolver.preflight(ResolveSkillRequest(config["skill"]["id"], tasks[0]["task"], {},
                                                 receipt["artifact_id"]))
    features = resolver.features[receipt["artifact_id"]]
    if not features:
        raise ValueError("the snapshot has no learned features")
    other = sorted(name for name, spec in features.items() if spec.get("scope") != "document")
    if other:
        raise ValueError(f"replay supports document-scope features only; not: {other}")
    return features, config["skill"]["resolver"]["options"]["judge_model"]


def compare(rows, saved):
    """Agreement tables and the list of disagreements; pure, so it can be tested without a model."""
    by_key = {}
    for row in rows:
        by_key.setdefault((row["set"], row["task_id"]), {})[row["mode"]] = row
    summary = {"modes": {}, "on_vs_off": {}, "replay_vs_saved": {}, "disagreements": [], "materials_mismatch": []}
    for mode in MODES:
        mine = [r for r in rows if r["mode"] == mode]
        summary["modes"][mode] = {
            "calls": len(mine), "unusable": sum(1 for r in mine if r.get("unusable")),
            "input_tokens": sum(r["input_tokens"] or 0 for r in mine),
            "output_tokens": sum(r["output_tokens"] or 0 for r in mine),
            "latency_seconds": sum(r["latency_seconds"] or 0 for r in mine),
            "answers": dict(Counter(v for r in mine for v in r["values"].values())),
        }
    for (name, task_id), modes in sorted(by_key.items()):
        stats = summary["on_vs_off"].setdefault(name, {"answers": 0, "same": 0, "changes": Counter()})
        if set(modes) == set(MODES):
            on, off = modes["on"]["values"], modes["off"]["values"]
            for feature in sorted(on):
                stats["answers"] += 1
                if on[feature] == off.get(feature):
                    stats["same"] += 1
                else:
                    stats["changes"][f"{on[feature]} -> {off.get(feature)}"] += 1
                    summary["disagreements"].append({"set": name, "task_id": task_id, "feature": feature,
                                                     "on": on[feature], "off": off.get(feature)})
        if name in saved and "on" in modes:
            check = summary["replay_vs_saved"].setdefault(name, {"answers": 0, "same": 0, "different": []})
            old = saved[name].get(task_id)
            if old is None:
                continue
            if old["materials_sha256"] and old["materials_sha256"] != modes["on"]["materials_sha256"]:
                summary["materials_mismatch"].append({"set": name, "task_id": task_id})
            for feature, value in sorted(modes["on"]["values"].items()):
                if feature in old["values"]:
                    check["answers"] += 1
                    if old["values"][feature] == value:
                        check["same"] += 1
                    else:
                        check["different"].append({"task_id": task_id, "feature": feature,
                                                   "saved": old["values"][feature], "replay": value})
    for stats in summary["on_vs_off"].values():
        stats["changes"] = dict(stats["changes"])
    return summary


def markdown(summary):
    lines = ["# Judge: thinking on vs off", "", "## Cost per mode", "",
             "| Mode | Calls | Unusable | Input tokens | Output tokens | Model time | Per call | Answers |",
             "|---|---|---|---|---|---|---|---|"]
    for mode, m in summary["modes"].items():
        per = m["latency_seconds"] / m["calls"] if m["calls"] else 0
        lines.append(f"| {mode} | {m['calls']} | {m['unusable']} | {m['input_tokens']:,} | {m['output_tokens']:,} "
                     f"| {m['latency_seconds'] / 60:.1f} min | {per:.1f} s | {m['answers']} |")
    lines += ["", "## Thinking on vs off", "", "| Set | Answers | Same | Changes |", "|---|---|---|---|"]
    for name, s in summary["on_vs_off"].items():
        lines.append(f"| {name} | {s['answers']} | {s['same']} | {s['changes'] or '-'} |")
    lines += ["", "## Replay (thinking on) vs the saved run", ""]
    for name, s in summary["replay_vs_saved"].items():
        lines.append(f"- {name}: {s['same']}/{s['answers']} answers reproduced; different: {s['different'] or 'none'}")
    lines.append(f"- materials hash mismatches: {summary['materials_mismatch'] or 'none'}")
    lines += ["", "## Disagreements (to check by hand)", ""]
    lines += [f"- {d['set']} {d['task_id']} `{d['feature']}`: on={d['on']}, off={d['off']}"
              for d in summary["disagreements"]] or ["- none"]
    return "\n".join(lines) + "\n"


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    parser.add_argument("--set", action="append", required=True, metavar="NAME=CONFIG")
    parser.add_argument("--saved", action="append", default=[], metavar="NAME=RUN_DIR")
    parser.add_argument("--out", required=True)
    parser.add_argument("--modes", default="on,off", help="comma-separated: on, off")
    parser.add_argument("--limit", type=int, help="first N documents of each set (for a smoke test)")
    args = parser.parse_args()

    modes = [m for m in args.modes.split(",") if m]
    if not modes or set(modes) - set(MODES):
        parser.error("--modes takes on and/or off")
    sets = dict(item.split("=", 1) for item in args.set)
    saved = {name: saved_answers(path) for name, path in (item.split("=", 1) for item in args.saved)}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    calls = out / "calls.jsonl"
    done = load_done(calls)

    features, judge_model = await learned_features(load_config(args.config))
    print(f"{len(features)} learned features: {sorted(features)}", flush=True)
    judges = {mode: FeatureJudge(judge_client(judge_model, thinking=mode == "on")) for mode in modes}

    for name, config_path in sets.items():
        config = load_config(config_path)
        adapter = factory(config["benchmark"])
        await adapter.prepare_runtime()
        tasks = select_tasks(adapter, config["run"])[:args.limit]
        for number, task in enumerate(tasks, 1):
            materials = adapter.judge_materials(task)
            context = judge_context(task["task"], materials)
            texts = {judged_text(spec, context) for spec in features.values()}
            assert len(texts) == 1, "document-scope features share one judged text"
            text = texts.pop()
            for mode in modes:
                if (name, task["id"], mode) in done:
                    continue
                values, response = await judges[mode].judge(features, text)
                meta, usage = response.metadata or {}, response.usage or {}
                row = {"set": name, "task_id": task["id"], "mode": mode, "values": values,
                       "unusable": meta.get("judge_unusable"), "finish_reason": meta.get("finish_reason"),
                       "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                       "latency_seconds": meta.get("latency_seconds"),
                       "materials_sha256": source_fingerprint(materials),
                       "text_sha256": source_fingerprint(text), "output": response.message}
                with calls.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                done[(name, task["id"], mode)] = row
                print(f"{name} {number}/{len(tasks)} {task['id']} thinking {mode}: "
                      f"{row['output_tokens']} out, {row['latency_seconds'] or 0:.1f} s, {values}", flush=True)

    summary = compare(list(done.values()), saved)
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    (out / "summary.md").write_text(markdown(summary), encoding="utf-8")
    print(markdown(summary), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
