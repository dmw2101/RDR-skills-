"""Passing worker views used as inspector examples and candidate replay inputs."""

import json

from skill_agent.contracts import fingerprint
from skill_agent.experiments import episode_filename
from skill_agent.storage import read_json

from .records import episode_key, skill_views, trace_steps


class PassingTraces:
    def __init__(self):
        self.by_task = {}
        self.by_node = {}

    def add_round(self, index, version, records, passed, tasks, materials):
        for record in records:
            task_id = record["task_id"]
            if passed.get(episode_key(record)) is not True or task_id not in tasks:
                continue
            task = tasks[task_id]
            events = record.get("events", [])
            views = skill_views(events, materials(task), include_markdown=True)
            self.by_task.setdefault(task_id, []).extend(views)
            for view in views:
                for node in view["terminal_ids"]:
                    examples = self.by_node.setdefault(node, {})
                    examples.pop(task_id, None)
                    examples[task_id] = (index, version, task, events, view)

    def examples(self, node, exclude_task):
        examples = []
        for index, version, task, events, view in reversed(list(self.by_node.get(node, {}).values())):
            if task["id"] == exclude_task:
                continue
            turn = view["turn"]
            steps = trace_steps(events, max(0, turn - 1), turn + 1, limit=500)
            markdown = view["markdown"]
            if len(markdown) > 1800:
                markdown = markdown[:900] + "\n...\n" + markdown[-900:]
            examples.append({
                "task_id": task["id"], "task": task["task"][:1000],
                "round": index + 1, "tree_version": version, "turn": turn,
                "context": view["context"][:1200], "guidance": markdown,
                "fragment_ids": view["fragment_ids"],
                "trace_excerpt": "\n".join(json.dumps(step, ensure_ascii=False) for step in steps)[:2000],
            })
            if len(examples) == 6:
                break
        return examples


def saved_passing_records(directory, passed):
    """Read individual verified records so one missing trace does not hide the others."""
    for source, ok in passed.items():
        if ok is not True:
            continue
        task, condition, replicate = source.rsplit("|", 2)
        path = directory / "run" / "episodes" / episode_filename(task, condition, int(replicate))
        try:
            record = read_json(path)
        except FileNotFoundError:
            continue
        if (episode_key(record) != source or record.get("record_sha256") != fingerprint(
                {k: v for k, v in record.items() if k != "record_sha256"})):
            raise ValueError(f"episode record changed: {path}")
        yield record


async def affected_tasks(traces, baseline, candidate, judge, excluded):
    """Compare guidance under both trees with the same facts for each saved view."""
    changed = []
    for task_id, views in traces.by_task.items():
        if task_id in excluded:
            continue
        seen = set()
        for view in views:
            if view["case"] is None:
                continue
            replay_key = fingerprint([view["case"], view["context"]])
            if replay_key in seen:
                continue
            seen.add(replay_key)
            case = await candidate.replay_case(view["case"], view["context"], judge)
            previous = {name: case[name] for name in baseline.schema.features}
            if baseline.render(previous).markdown != candidate.render(case).markdown:
                changed.append(task_id)
                break
    return changed
