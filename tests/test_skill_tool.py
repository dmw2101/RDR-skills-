from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from rdr_engine.skill_cases import build_case
from rdr_engine.skill_compiler import FeatureSchema, SkillBundle
from rdr_engine.skill_tool import main


ROOT = Path(__file__).resolve().parents[1]


class SkillToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.skill = Path(self.temp.name) / "alfworld"
        (self.skill / "compiled").mkdir(parents=True)
        baseline = ROOT / "skills" / "alfworld"
        shutil.copyfile(baseline / "initial.md", self.skill / "initial.md")
        ir_path = self.skill / "compiled" / "ir.json"
        shutil.copyfile(baseline / "compiled" / "ir.json", ir_path)
        schema = FeatureSchema.from_file(ROOT / "skills" / "feature_schemas.json", "alfworld")
        SkillBundle.build(self.skill, ir_path=ir_path, feature_schema=schema)
        self.case_path = Path(self.temp.name) / "case.json"

    def test_case_file_supports_utf8_and_powershell_utf8_bom(self) -> None:
        for encoding in ("utf-8", "utf-8-sig"):
            with self.subTest(encoding=encoding):
                self.case_path.write_text('{"task_type":["heat_place"]}', encoding=encoding)
                output, diagnostics = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(diagnostics):
                    status = main([
                        "render", str(self.skill), "--case-file", str(self.case_path), "--show-path",
                    ])
                self.assertEqual(status, 0)
                self.assertIn("| Heat & Place |", output.getvalue())
                self.assertNotIn("| Cool & Place |", output.getvalue())
                self.assertIn('case={"task_type": ["heat_place"]}', diagnostics.getvalue())
                self.assertIn("terminals=[1, 6]", diagnostics.getvalue())
                self.assertIn("    6:true", diagnostics.getvalue())

    def test_case_file_rejects_invalid_json_and_invalid_features(self) -> None:
        for contents, message in (
            ("{task_type:heat_place}", "Expecting property name"),
            ("[]", "a skill case must be a JSON object"),
            ("{}", "missing required case feature"),
            ('{"task_type":"not_a_task"}', "invalid value"),
        ):
            with self.subTest(contents=contents):
                self.case_path.write_text(contents, encoding="utf-8")
                output, diagnostics = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(diagnostics):
                    with self.assertRaises(SystemExit) as failure:
                        main(["render", str(self.skill), "--case-file", str(self.case_path)])
                self.assertEqual(failure.exception.code, 1)
                self.assertEqual(output.getvalue(), "")
                self.assertIn(message, diagnostics.getvalue())

    def test_missing_case_file_reports_the_path(self) -> None:
        diagnostics = io.StringIO()
        with redirect_stderr(diagnostics):
            with self.assertRaises(SystemExit) as failure:
                main(["render", str(self.skill), "--case-file", str(self.case_path)])
        self.assertEqual(failure.exception.code, 1)
        self.assertIn(repr(str(self.case_path)), diagnostics.getvalue())

    def test_case_file_cannot_be_combined_with_task_or_inline_case(self) -> None:
        for alternative in (("--task", "Heat an apple"), ("--case", "{}")):
            with self.subTest(alternative=alternative), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as failure:
                    main([
                        "render", str(self.skill), "--case-file", str(self.case_path), *alternative,
                    ])
                self.assertEqual(failure.exception.code, 2)

    def test_single_classification_bundle_asks_for_recompilation(self) -> None:
        manifest_path = self.skill / "compiled" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        diagnostics = io.StringIO()
        with redirect_stderr(diagnostics), self.assertRaises(SystemExit) as failure:
            main(["render", str(self.skill), "--task", "Heat an apple"])
        self.assertEqual(failure.exception.code, 1)
        self.assertIn("recompile", diagnostics.getvalue())

    def test_alfworld_combined_goals_include_both_workflows(self) -> None:
        output, diagnostics = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostics):
            main(["render", str(self.skill), "--task",
                  "Clean a plate and place it on the table, then heat an apple and put it in a bowl",
                  "--show-path"])
        case = json.loads(diagnostics.getvalue().splitlines()[0].removeprefix("case="))
        self.assertEqual(case, {"task_type": ["clean_place", "heat_place"]})
        self.assertIn("| Clean & Place |", output.getvalue())
        self.assertIn("| Heat & Place |", output.getvalue())
        self.assertNotIn("| Pick & Place |", output.getvalue())


class PDFSkillToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.skill = Path(self.temp.name) / "pdf"
        (self.skill / "compiled").mkdir(parents=True)
        baseline = ROOT / "skills" / "pdf"
        shutil.copyfile(baseline / "initial.md", self.skill / "initial.md")
        self.schema = FeatureSchema.from_file(ROOT / "skills" / "feature_schemas.json", "pdf")
        SkillBundle.build(
            self.skill, ir_path=baseline / "compiled" / "ir.reviewed.json",
            feature_schema=self.schema,
        )

    def test_natural_language_queries_select_different_skill_sections(self) -> None:
        for task, operation, heading, excluded in (
            ("extract text from pdf", "extract_content", "#### Extract Text with Layout", "#### Rotate Pages"),
            ("Merge these PDF files", "combine_or_split", "#### Merge PDFs", "### reportlab"),
            ("Split this PDF into separate files", "combine_or_split", "#### Split PDF", "### reportlab"),
            ("Rotate the first PDF page by 90 degrees", "rotate_pages", "#### Rotate Pages", "#### Extract Text with Layout"),
            ("Create a PDF report", "create_or_watermark", "#### Basic PDF Creation", "#### Rotate Pages"),
            ("Add a watermark to this PDF", "create_or_watermark", "### Add Watermark", "#### Rotate Pages"),
            ("Remove the password from this PDF", "password_security", "### Password Protection", "#### Extract Text with Layout"),
            ("Encrypt this PDF", "password_security", "### Password Protection", "### reportlab"),
        ):
            with self.subTest(task=task):
                output, diagnostics = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(diagnostics):
                    status = main(["render", str(self.skill), "--task", task, "--show-path"])
                self.assertEqual(status, 0)
                case = json.loads(diagnostics.getvalue().splitlines()[0].removeprefix("case="))
                self.assertEqual(case, {"operation": [operation], "source_kind": "unknown"})
                self.assertIn(heading, output.getvalue())
                self.assertNotIn(excluded, output.getvalue())

    def test_scanned_query_adds_ocr_guidance(self) -> None:
        for task, operation, source_kind, includes_ocr in (
            ("Extract text from a scanned PDF", ["extract_content"], "scanned_image", True),
            ("OCR this PDF", [], "scanned_image", True),
            ("Make this scanned PDF searchable", ["extract_content"], "scanned_image", True),
            ("Extract text from a text-based PDF", ["extract_content"], "text_based", False),
            ("Extract text from a mixed PDF", ["extract_content"], "mixed", False),
            ("Extract text from a PDF", ["extract_content"], "unknown", False),
        ):
            with self.subTest(task=task):
                output, diagnostics = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(diagnostics):
                    main(["render", str(self.skill), "--task", task, "--show-path"])
                case = json.loads(diagnostics.getvalue().splitlines()[0].removeprefix("case="))
                self.assertEqual(case, {"operation": operation, "source_kind": source_kind})
                self.assertEqual("### Extract Text from Scanned PDFs" in output.getvalue(), includes_ocr)
                if not operation:
                    self.assertNotIn("#### Extract Text with Layout", output.getvalue())

    def test_multiple_unknown_and_explicit_context(self) -> None:
        for task, operation in (
            ("", []),
            ("Help with my PDF", []),
            ("Merge and rotate these PDFs", ["combine_or_split", "rotate_pages"]),
            ("Read this password-protected PDF", ["extract_content"]),
            ("Fill out this PDF form", ["fill_forms"]),
        ):
            with self.subTest(task=task):
                self.assertEqual(
                    build_case("pdf", task, None, self.schema),
                    {"operation": operation, "source_kind": "unknown"},
                )
        self.assertEqual(
            build_case("pdf", "Help with my PDF", {
                "operation": ["extract_content"], "source_kind": "scanned_image",
            }, self.schema),
            {"operation": ["extract_content"], "source_kind": "scanned_image"},
        )

    def test_combined_query_and_case_file_render_both_operations(self) -> None:
        self.case_path = Path(self.temp.name) / "combined.json"
        self.case_path.write_text(json.dumps({
            "operation": ["extract_content", "rotate_pages"], "source_kind": "unknown",
        }), encoding="utf-8-sig")
        results = []
        for arguments in (
            ["--task", "Extract text and rotate the first PDF page"],
            ["--case-file", str(self.case_path)],
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["render", str(self.skill), *arguments]), 0)
            text = output.getvalue()
            self.assertIn("#### Extract Text with Layout", text)
            self.assertIn("#### Rotate Pages", text)
            self.assertNotIn("### Add Watermark", text)
            self.assertEqual(text.count("### qpdf"), 1)
            results.append(text)
        self.assertEqual(results[0], results[1])


if __name__ == "__main__":
    unittest.main()
