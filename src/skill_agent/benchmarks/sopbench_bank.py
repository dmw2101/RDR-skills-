"""SOPBench Bank fixed-policy subset, isolated sessions and upstream evaluation."""

import copy
import hashlib
import importlib
import inspect
import json
import statistics
import subprocess
import sys
import types
from pathlib import Path

from ..contracts import fingerprint
from ..storage import read_json, write_json


REVISION = "8790ed7406acbd22270f90bd1c4038de3fc5db8f"
REPAIR_VERSION = "bank-representation-auth-strict-arguments-v2"
SYSTEM_PROMPT = (
    "You are a bank customer-service assistant. Respond to the customer's request using the "
    "available Bank tools. Ask the customer for information when needed. Call exit_conversation "
    "when the request is complete or cannot be completed."
)


class BankEnvironmentError(RuntimeError):
    """A Bank tool crashed internally rather than returning a benchmark result."""


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


def normalize_bank_task(original):
    """Adapt Bank's list-shaped task data to the dictionary its tools expect.

    This modifies an in-memory copy for both the environment and scorer; the
    pinned upstream dataset on disk is never changed. Fail on ambiguous data.
    """
    task = copy.deepcopy(original)
    for account in task["initial_database"]["accounts"].values():
        cards = account.get("credit_cards")
        if isinstance(cards, list):
            converted = {}
            for card in cards:
                if not isinstance(card, dict) or not isinstance(card.get("card_number"), str):
                    raise ValueError("Bank credit card entry has no string card_number")
                number = card["card_number"]
                if number in converted:
                    raise ValueError("Bank credit card numbers are not unique")
                converted[number] = {k: copy.deepcopy(v) for k, v in card.items() if k != "card_number"}
            account["credit_cards"] = converted
        elif not isinstance(cards, dict):
            raise ValueError("Bank credit_cards must be a list or dictionary")
    return task


def repaired_bank_classes():
    """Create scoped subclasses; do not monkey-patch the upstream checkout."""
    from env.domains.bank.bank import Bank, Bank_Strict

    class RepairedBank(Bank):
        def get_credit_cards(self, username: str) -> tuple[bool, list]:
            result = super().get_credit_cards(username)
            if not isinstance(result, tuple) or len(result) != 2:
                return result
            success, cards = result
            if success and isinstance(cards, dict):
                return True, [{"card_number": number, **copy.deepcopy(details)}
                              for number, details in cards.items()]
            return result

        def apply_credit_card(self, username: str, total_assets: float, monthly_income: float) -> bool:
            if not self.domain_dep.process(method_str="apply_credit_card", username=username,
                                           total_assets=total_assets, monthly_income=monthly_income):
                return False
            # A stable digest keeps simulator and scorer consistent across
            # processes, including when a completed run is resumed later.
            digits = "".join(str(int(hashlib.sha256(
                f"{username}{total_assets}{monthly_income}{i}".encode("utf-8")
            ).hexdigest(), 16) % 10) for i in range(16))
            number = " ".join(digits[i:i + 4] for i in range(0, 16, 4))
            cards = self.accounts[username].setdefault("credit_cards", {})
            # Upstream only inserts when the card already exists (inverted check).
            if number not in cards:
                cards[number] = {"credit_limit": monthly_income / 4, "credit_balance": 0.0}
            return True

    class RepairedBankStrict(Bank_Strict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.domain_system.__class__ = RepairedBank

        def login_user(self, username, identification=None):
            if not self.domain_dep.process(method_str="login_user", username=username,
                                           identification=identification):
                return False
            success = self.domain_system.login_user(username, identification)
            # Upstream sets logged-in state even when authentication fails.
            if success:
                self.state_tracker.set_login_user(username)
            return success

    return RepairedBank, RepairedBankStrict


class BankTool:
    def __init__(self, session, definition):
        self.session = session
        self.definition = copy.deepcopy(definition)
        self.name = definition["function"]["name"]

    async def execute(self, arguments):
        # Keep model-supplied values unchanged, as in the upstream Bank tool.
        effective_arguments = copy.deepcopy(arguments)
        try:
            function = getattr(self.session.domain_system, self.name)
        except AttributeError as exc:
            raise BankEnvironmentError(f"Bank tool {self.name} is missing") from exc
        try:
            inspect.signature(function).bind(**effective_arguments)
        except TypeError as exc:
            # Invalid tool arguments are attributable to the model's call.
            result = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            try:
                result = function(**effective_arguments)
                # Preserve values while converting upstream tuples to JSON arrays.
                result = json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
            except Exception as exc:
                # Upstream tool crashes are not evidence of a model error. The
                # runner can retry or leave this episode explicitly unresolved.
                raise BankEnvironmentError(f"Bank tool {self.name} crashed: {type(exc).__name__}: {exc}") from exc
        call = {"tool_name": self.name, "arguments": copy.deepcopy(effective_arguments),
                "content": copy.deepcopy(result)}
        self.session.func_calls.append(call)
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

    def __init__(self, *, source_dir, skill_dir="skills/sopbench_bank_fixed_policy", restore_missing_tools=False):
        self.root = Path(source_dir).resolve()
        self.skill = Path(skill_dir).resolve()
        self.task_module, self.evaluator = upstream_modules(self.root)
        self.repaired_bank, repaired_strict = repaired_bank_classes()
        original_evaluate = self.evaluator.evaluator_function_directed_graph
        scorer_globals = dict(original_evaluate.__globals__)
        scorer_globals["domain_keys"] = {**scorer_globals["domain_keys"], "bank_strict": repaired_strict}
        # Clone only this function's globals: other SOPBench domains and upstream
        # modules retain their original classes and behaviour.
        self.evaluate = types.FunctionType(original_evaluate.__code__, scorer_globals,
                                           original_evaluate.__name__, original_evaluate.__defaults__,
                                           original_evaluate.__closure__)
        if type(restore_missing_tools) is not bool:
            raise ValueError("restore_missing_tools must be a boolean")
        self.restore_missing_tools = restore_missing_tools
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
            self.selected[task_id] = dict(normalize_bank_task(original), user_goal=action)
        if len(self.selected) != 34:
            raise ValueError("expected the 34-case fixed-policy subset")
        self.defaults = self.task_module.task_default_dep_full(
            "bank", "full", "structured", dependency_verb_dep_orig=True)
        files = {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted((self.root / "env").rglob("*.py"))}
        provenance = {
            "revision": REVISION, "env_files": files, "data": self.policy["source_data_sha256"],
            "selection": selection, "policy": self.policy_sha,
            "protocol": "fixed-policy-bank-public-context-v2",
            "adaptations": REPAIR_VERSION,
            "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        if self.restore_missing_tools:
            provenance["restored_tools"] = ["cancel_credit_card", "pay_bill_with_credit_card"]
        self.snapshot_fingerprint = fingerprint(provenance)

    async def prepare_runtime(self):
        return {"runtime": "sopbench-bank-python", "source_revision": REVISION,
                "python": sys.version, "customer": "official non-LLM default response",
                "cases": len(self.selected), "policy_sha256": self.policy_sha,
                "benchmark_adaptations": REPAIR_VERSION,
                **({"restored_tools": ["cancel_credit_card", "pay_bill_with_credit_card"]}
                   if self.restore_missing_tools else {})}

    def tasks(self, split, limit=None):
        if split != "dev":
            raise ValueError("the 34-case fixed-policy selection is available as dev only; it is not an official split")
        result = [{"id": name, "task": task["user_prompt"]} for name, task in self.selected.items()]
        return result if limit is None else result[:limit]

    async def open_session(self, public_task):
        if public_task["id"] not in self.selected:
            raise ValueError("task is not in the selected Bank subset")
        task = copy.deepcopy(self.selected[public_task["id"]])
        if public_task["task"] != task["user_prompt"]:
            raise ValueError("public Bank task text changed")
        domain, user_info, assistant_info, _ = self.task_module.task_initializer(
            "bank", task, *self.defaults, None, "prompt", False, "structured")
        domain.__class__ = self.repaired_bank
        policy_bytes = (assistant_info["instructions"].rstrip() + "\n").encode("utf-8")
        if hashlib.sha256(policy_bytes).hexdigest() != self.policy_sha:
            raise ValueError("case does not use the selected shared policy")
        if self.restore_missing_tools:
            from env.domains.bank import bank_assistant
            names = {item["function"]["name"] for item in assistant_info["tools"]}
            for name, parameters in (
                ("cancel_credit_card", {"username": "string", "card_number": "string"}),
                ("pay_bill_with_credit_card", {"username": "string", "amount": "number", "card_number": "string"}),
            ):
                if name in names or not callable(getattr(domain, name, None)):
                    raise ValueError(f"cannot restore missing Bank tool {name}")
                assistant_info["tools"].append({"type": "function", "function": {
                    "name": name,
                    "description": bank_assistant.action_descriptions[name] + " " + bank_assistant.action_returns[name],
                    "strict": True,
                    "parameters": {"type": "object", "properties": {
                        key: {"type": kind, "description": bank_assistant.action_param_descriptions[key]}
                        for key, kind in parameters.items()},
                        "required": list(parameters), "additionalProperties": False},
                }})
        return BankSession(domain_system=domain, user_info=user_info, assistant_info=assistant_info, task=task)

    def score(self, records, output_dir):
        incomplete = [f"{record['task_id']}:{record['condition']}:{record.get('status')}"
                      for record in records if record.get("status") != "completed"]
        if incomplete:
            raise ValueError("Cannot score incomplete Bank episodes: " + ", ".join(incomplete))
        if any(not isinstance(record.get("prediction"), dict) for record in records):
            raise ValueError("Cannot score a completed Bank episode without a prediction")
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
                evaluation = self.evaluate(
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
                  "benchmark_adaptations": REPAIR_VERSION,
                  "scoring_policy": "only completed episodes are scored; upstream evaluator uses the documented Bank adaptations"}
        write_json(output_dir / "scoring-result.json", result)
        return result
