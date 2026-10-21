"""Read upstream artifacts and replace MAP output, without changing upstream files."""

from pathlib import Path

from ._json import ValidationError, read, write
from .patches import FailedExecution, PatchRequest, RecordedPatchProvider, create_patch


def _one(records, field, task_id):
    if type(records) is not list:
        raise ValidationError("Expected an upstream record list")
    matches = [r for r in records if type(r) is dict and str(r.get(field)) == task_id]
    if len(matches) != 1:
        raise ValidationError(f"Expected exactly one upstream record for task {task_id}")
    return matches[0]


def load_failure(*, task_id, context, skill_text, evaluation_file, parsed_errors_file, analysis_dir):
    """Require actual comparison failure plus the analyst's verified-repair flag.

    An error-analysis record alone is insufficient: upstream can also parse flat
    Markdown reports that have no verification flag.
    """
    task_id = str(task_id)
    evaluation_file, parsed_errors_file, analysis_dir = (Path(p).resolve(strict=True)
                                                        for p in (evaluation_file, parsed_errors_file, analysis_dir))
    if analysis_dir.name != task_id:
        raise ValidationError("The repair directory must belong to the source task ID")
    flag = analysis_dir / "evaluate_passed.flag"
    report = analysis_dir / "analysis_report.md"
    if not flag.is_file() or not report.is_file():
        raise ValidationError("Missing independently verified analyst repair/report")
    evaluation = read(evaluation_file)
    if type(evaluation) is not dict:
        raise ValidationError("Expected upstream eval_official_results.json")
    verdict = _one(evaluation.get("results"), "id", task_id)
    analysis = _one(read(parsed_errors_file), "instance_id", task_id)
    return FailedExecution(task_id, context, skill_text, analysis,
                           {"evaluation_file": str(evaluation_file), "analysis_file": str(parsed_errors_file),
                            "repair_flag": str(flag), "verdict": verdict})


def verify_source(failure):
    """Recheck the linked local artifacts before exporting or staging a proposal."""
    source = failure.source
    refreshed = load_failure(task_id=failure.task_id, context=failure.context, skill_text=failure.skill_text,
                             evaluation_file=source["evaluation_file"], parsed_errors_file=source["analysis_file"],
                             analysis_dir=Path(source["repair_flag"]).parent)
    if refreshed.to_dict() != failure.to_dict():
        raise ValidationError("Failure source artifacts no longer match this proposal")


class ExceptionMapCreator:
    """The new MAP boundary. Inputs are upstream analyses; outputs are exceptions.

    Upstream execution, scoring, triage, and verified analysis stay unchanged.
    This replaces ParallelSkillEvolver's proposal prompt/response contract, rather
    than calling its reducer or converting its textual output in another call.
    Live backends deliberately remain disabled during the offline phase.
    """

    def run_map_phase(self, registry, failures, recordings, output_dir):
        if len(failures) != len(recordings) or not failures:
            raise ValidationError("Supply one explicit MAP recording for each failed execution")
        destination = Path(output_dir)
        if destination.exists():
            raise ValidationError("Export requires a new directory")
        proposals = []
        # Validate the entire batch before publishing any proposals. No hidden
        # partial-success behavior and no automatic response-repair calls.
        for failure, recording in zip(failures, recordings):
            verify_source(failure)
            request = PatchRequest(registry, failure)
            proposal = create_patch(request, provider=RecordedPatchProvider(recording))
            proposals.append(proposal)
        for index, proposal in enumerate(proposals, 1):
            write(destination / f"proposal_{index:04d}.json", proposal.to_dict())
            write(destination / f"request_{index:04d}.json", proposal.request.to_dict())
        write(destination / "summary.json", {"stage": "MAP_only", "proposals": len(proposals), "model_calls": 0,
                                           "reducer_called": False, "proposal_effectiveness": "not_measured"})
        return proposals
