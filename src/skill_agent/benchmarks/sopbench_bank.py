"""SOPBench Bank fixed-policy subset, isolated sessions and upstream evaluation."""

import copy
import hashlib
import importlib
import json
import statistics
import subprocess
import sys
from pathlib import Path

from ..contracts import fingerprint
from ..storage import read_json, write_json


REVISION = "8790ed7406acbd22270f90bd1c4038de3fc5db8f"
SYSTEM_PROMPT = (
    "You are a bank customer-service assistant. Respond to the customer's request using the "
    "available Bank tools. Ask the customer for information when needed. Call exit_conversation "
    "when the request is complete or cannot be completed."
)


def upstream_modules(root):
    root = Path(root).resolve()
    if not (root / "env/task.py").is_file() or not (root / "data/bank_tasks.json").is_file():
        raise ValueError("SOPBench checkout is missing env/ or data/; run scripts/prepare_sopbench_bank.py")
    revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISION:
        raise ValueError("SOPBench checkout does not match the pinned revision")
    subprocess.run(["git", "-C", str(root), "diff", "--exit-code", "HEAD", "--", "env", "data/bank_tasks.json"],
                   check=True, capture_output=True)
    sys.path.insert(0, str(root))
    task_module = importlib.import_module("env.task")
    if not Path(task_module.__file__).resolve().is_relative_to(root):
        raise ValueError("an unrelated Python env package is already loaded")
    evaluator = importlib.import_module("env.evaluator")
    return task_module, evaluator


class BankTool:
    def __init__(self, session, definition):
        self.session = session
        self.definition = copy.deepcopy(definition)
        self.name = definition["function"]["name"]

    async def execute(self, arguments):
        try:
            result = getattr(self.session.domain_system, self.name)(**arguments)
            # Preserve values while converting upstream tuples to JSON arrays.
            result = json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        self.session.func_calls.append({"tool_name": self.name, "arguments": copy.deepcopy(arguments),
                                        "content": copy.deepcopy(result)})
        return result


class ExitConversation:
    definition = {"type": "function", "function": {
        "name": "exit_conversation",
        "description": "End the conversation when the request is complete or cannot be completed.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}

    def __init__(self, session):
        self.session = session

    async def execute(self, arguments):
        if arguments:
            raise ValueError("exit_conversation takes no arguments")
        self.session.finished = True
        return {"finished": True}


class BankSession:
    collect_on_failure = True
    require_complete_responses = True

    def __init__(self, *, domain_system, user_info, assistant_info, task):
        self.domain_system = domain_system
        self.prompt = task["user_prompt"]
        self.user_known = copy.deepcopy(user_info["known"])
        self.finished = False
        self.func_calls = []
        self.tools = {definition["function"]["name"]: BankTool(self, definition)
                      for definition in assistant_info["tools"]}
        self.tools["exit_conversation"] = ExitConversation(self)

    async def customer_response(self, message):
        # Match the upstream non-LLM customer: reveal only user_known after the assistant speaks.
        return ("Here is all the information I can provide:\n"
                + json.dumps(self.user_known, indent=4, ensure_ascii=False)
                + "\n\nIf you have completed my request or cannot assist me with this request, "
                  "please use the `exit_conversation` action to end our conversation.")

    def record_invalid_tool_call(self, name, arguments, output):
        self.func_calls.append({"tool_name": name, "arguments": copy.deepcopy(arguments),
                                "content": copy.deepcopy(output)})

    async def collect(self):
        return {"final_database": copy.deepcopy(self.domain_system.evaluation_get_database()),
                "func_calls": copy.deepcopy(self.func_calls)}

    async def close(self):
        pass


class SOPBenchBankAdapter:
    system_prompt = SYSTEM_PROMPT

    def __init__(self, *, source_dir, skill_dir="skills/sopbench_bank_fixed_policy"):
        self.root = Path(source_dir).resolve()
        self.skill = Path(skill_dir).resolve()
        self.task_module, self.evaluator = upstream_modules(self.root)
        self.policy = read_json(self.skill / "source.json")
        selection = read_json(self.skill / "policy_cases.json")
        self.policy_sha = self.policy["initial_sha256"]
        if selection["policy_sha256"] != self.policy_sha:
            raise ValueError("Bank case list and policy do not match")
        if hashlib.sha256((self.skill / "initial.md").read_bytes()).hexdigest() != self.policy_sha:
            raise ValueError("Bank source policy changed")
        self.raw_tasks = read_json(self.root / "data/bank_tasks.json")
        if hashlib.sha256((self.root / "data/bank_tasks.json").read_bytes()).hexdigest() != self.policy["source_data_sha256"]:
            raise ValueError("Bank task data changed from the selected snapshot")
        self.selected = {}
        for reference in selection["cases"]:
            action, index = reference["action"], reference["index"]
            original = self.raw_tasks[action][index]
            actual = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
            if actual != reference["task_sha256"]:
                raise ValueError("Bank task reference changed")
            task_id = f"{action}-{index:03d}"
            if task_id in self.selected:
                raise ValueError("duplicate Bank task reference")
            self.selected[task_id] = dict(copy.deepcopy(original), user_goal=action)
        if len(self.selected) != 34:
            raise ValueError("expected the 34-case fixed-policy subset")
        self.defaults = self.task_module.task_default_dep_full(
            "bank", "full", "structured", dependency_verb_dep_orig=True)
        files = {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted((self.root / "env").rglob("*.py"))}
        self.snapshot_fingerprint = fingerprint({
            "revision": REVISION, "env_files": files, "data": self.policy["source_data_sha256"],
            "selection": selection, "policy": self.policy_sha,
            "protocol": "fixed-policy-bank-public-context-v1",
        })

    async def prepare_runtime(self):
        return {"runtime": "sopbench-bank-python", "source_revision": REVISION,
                "python": sys.version, "customer": "official non-LLM default response",
                "cases": len(self.selected), "policy_sha256": self.policy_sha}

    def tasks(self, split, limit=None):
        if split != "dev":
            raise ValueError("the 34-case fixed-policy selection is available as dev only; it is not an official split")
        result = [{"id": name, "task": task["user_prompt"]} for name, task in self.selected.items()]
        return result if limit is None else result[:limit]

    def rolling_skill_policy(self):
        from ..bank_selection import BankRollingSkillPolicy
        return BankRollingSkillPolicy()

    async def open_session(self, public_task):
        if public_task["id"] not in self.selected:
            raise ValueError("task is not in the selected Bank subset")
        task = copy.deepcopy(self.selected[public_task["id"]])
        if public_task["task"] != task["user_prompt"]:
            raise ValueError("public Bank task text changed")
        domain, user_info, assistant_info, _ = self.task_module.task_initializer(
            "bank", task, *self.defaults, None, "prompt", False, "structured")
        policy_bytes = (assistant_info["instructions"].rstrip() + "\n").encode("utf-8")
        if hashlib.sha256(policy_bytes).hexdigest() != self.policy_sha:
            raise ValueError("case does not use the selected shared policy")
        return BankSession(domain_system=domain, user_info=user_info, assistant_info=assistant_info, task=task)

    def score(self, records, output_dir):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        metrics, detailed = [], []
        for record in records:
            task = self.selected[record["task_id"]]
            prediction = record.get("prediction")
            evaluation = None
            if prediction is not None:
                # The evaluator uses sender entries only to count conversation messages.
                log = [{"sender": "assistant"} for e in record["events"] if e["type"] == "model_response"]
                log.extend({"sender": "user"} for e in record["events"] if e["type"] == "user_message")
                log.append({"sender": "assistant"})
                evaluation = self.evaluator.evaluator_function_directed_graph(
                    "bank", copy.deepcopy(task), log, prediction["func_calls"],
                    {"final_database": prediction["final_database"]}, "full")
            success = bool(record["status"] == "completed" and evaluation and evaluation["success"])
            metrics.append({"task_id": record["task_id"], "condition": record["condition"],
                            "replicate": record["replicate"], "metric": "sopbench_success",
                            "numerator": int(success), "denominator": 1})
            detailed.append({"task_id": record["task_id"], "condition": record["condition"],
                             "replicate": record["replicate"], "status": record["status"],
                             "effective_success": success, "official_evaluation": evaluation})
        summaries = []
        for condition, replicate in sorted({(r["condition"], r["replicate"]) for r in records}):
            group = [r for r in records if (r["condition"], r["replicate"]) == (condition, replicate)]
            scores = [d for d in detailed if (d["condition"], d["replicate"]) == (condition, replicate)]
            token_values, costs, incomplete = [], [], 0
            for record in group:
                events = record["events"]
                model_responses = [e for e in events if e["type"] == "model_response"]
                skills = [e for e in events if e["type"] == "skill"]
                skill_errors = [e for e in events if e["type"] == "skill_error"]
                usages = ([e.get("usage", {}) for e in model_responses] + [e["response"].get("usage", {}) for e in skills]
                          + [e["usage"] for e in skill_errors if "usage" in e])
                complete = (sum(e["type"] == "model_request" for e in events) == len(model_responses)
                            and sum(e["type"] == "skill_request" for e in events) == len(skills) + len(skill_errors)
                            and all("usage" in e for e in skill_errors))
                values = [u.get("input_tokens") for u in usages]
                if complete and all(type(v) in (int, float) for v in values):
                    token_values.append(sum(values))
                else:
                    incomplete += 1
                money = [u.get("cost_usd") for u in usages]
                if complete and all(type(v) in (int, float) for v in money):
                    costs.append(sum(money))
            successes = sum(d["effective_success"] for d in scores)
            summaries.append({"condition": condition, "replicate": replicate, "cases": len(group),
                              "completed": sum(r["status"] == "completed" for r in group),
                              "successes": successes, "success_rate": successes / len(group),
                              "mean_input_tokens": statistics.mean(token_values) if token_values else None,
                              "median_input_tokens": statistics.median(token_values) if token_values else None,
                              "episodes_with_incomplete_input_usage": incomplete,
                              "total_cost_usd": sum(costs) if len(costs) == len(group) else None,
                              "mean_seconds": statistics.mean(r["seconds"] for r in group)})
        result = {"summaries": summaries, "document_metrics": metrics,
                  "details": detailed, "scorer_revision": REVISION,
                  "scoring_policy": "incomplete episodes count as failures; observed official scores retained"}
        write_json(output_dir / "scoring-result.json", result)
        return result
