"""Local PDF tool sessions using a Python venv and fresh per-attempt folders."""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


PDF_PACKAGES = {
    "pypdf": "pypdf", "pdfplumber": "pdfplumber", "reportlab": "reportlab",
    "pytesseract": "pytesseract", "pdf2image": "pdf2image", "Pillow": "PIL",
}
PDF_COMMANDS = {"pdftotext": "-v", "pdfinfo": "-v", "pdftoppm": "-v",
                "tesseract": "--version", "qpdf": "--version"}


class OutputLimitError(RuntimeError):
    def __init__(self, message, partial=b""):
        super().__init__(message)
        self.partial = partial  # The output read before the limit was hit.


# Decision H1: like Trace2Skill's ReAct agent, the worker sees at most the first and last 3,000
# characters of a command's output, with a note on what was left out.
OBSERVATION_CHARS = 6000


def cut_output(text, limit=OBSERVATION_CHARS):
    """Keep the first and last limit/2 characters; say how much was left out and how to see it."""
    if limit is None or len(text) <= limit:
        return text
    half = limit // 2
    note = (f"\n\n[Output cut: showing the first {half:,} and last {half:,} characters; "
            f"{len(text) - 2 * half:,} characters left out. Save long output to a file and read parts "
            "of it with head, tail or grep.]\n\n")
    return text[:half] + note + text[-half:]


async def process(*args, timeout=60, limit=128_000, cwd=None, env=None):
    """Bound command duration/output and terminate descendants on interruption."""
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
               else {"start_new_session": True})
    child = await asyncio.create_subprocess_exec(
        *map(str, args), cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, **options)
    chunks, size = [], 0

    async def read():
        nonlocal size
        while block := await child.stdout.read(8192):
            size += len(block)
            if size > limit:
                chunks.append(block[: max(0, len(block) - (size - limit))])
                raise OutputLimitError("command output budget exceeded", b"".join(chunks))
            chunks.append(block)
        await child.wait()

    try:
        await asyncio.wait_for(read(), timeout)
    except BaseException:
        if os.name == "nt" and child.returncode is None:
            cleanup = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(child.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await cleanup.wait()
        elif os.name != "nt":
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if child.returncode is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
        await child.wait()
        # Close the pipes now: the attempt may go on, and a transport left for garbage collection
        # after its event loop has closed raises warnings on Windows.
        transport = getattr(child, "_transport", None)
        if transport is not None:
            transport.close()
        raise
    return child.returncode, b"".join(chunks).decode("utf-8", errors="replace")


def python_path(executable=None):
    executable = str(executable or sys.executable)
    # Do not resolve symlinks: a Linux venv's python often links to base Python.
    return os.path.abspath(shutil.which(executable) or executable)


def command_environment(python_executable):
    environment = os.environ.copy()
    directory = Path(python_executable).parent
    environment["PATH"] = str(directory) + os.pathsep + environment.get("PATH", "")
    environment["VIRTUAL_ENV"] = str(directory.parent)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    # Avoid passing provider credentials to ordinary tool subprocesses.
    for name in list(environment):
        if any(word in name.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            del environment[name]
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


async def venv_identity(python_executable=None):
    executable = python_path(python_executable)
    probe = (
        "import importlib, importlib.metadata as m, json, platform, sys; "
        "[importlib.import_module(name) for name in " + repr(list(PDF_PACKAGES.values())) + "]; "
        "print(json.dumps({'python': sys.version, 'platform': platform.platform(), "
        "'venv': sys.prefix != sys.base_prefix, 'prefix': sys.prefix, "
        "'packages': dict(sorted((d.metadata['Name'], d.version) for d in m.distributions()))}))"
    )
    code, output = await process(executable, "-c", probe,
                                 env=command_environment(executable), limit=1_000_000)
    if code:
        raise ValueError("PDF venv dependencies are unavailable; install .[pdf-runtime]. " + output[-2000:])
    identity = json.loads(output)
    if not identity["venv"]:
        raise ValueError("run with a venv's Python, or set benchmark.options.python_executable to it")
    identity.update({"runtime": "local-venv", "python_executable": executable,
                     "shell": "powershell" if os.name == "nt" else "/bin/sh"})
    identity["system_tools"] = {}
    for name, argument in PDF_COMMANDS.items():
        path = shutil.which(name)
        if path:
            code, output = await process(path, argument)
            identity["system_tools"][name] = {"path": path, "exit_code": code, "version": output.strip()}
        else:
            identity["system_tools"][name] = None
    return identity


class ShellTool:
    definition = {"type": "function", "function": {
        "name": "shell", "description": "Run a command in the local PDF working folder using the configured Python venv.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                       "required": ["command"], "additionalProperties": False}}}

    def __init__(self, session):
        self.session = session

    async def execute(self, arguments):
        if set(arguments) != {"command"} or not isinstance(arguments["command"], str):
            raise ValueError("shell expects a command string")
        if self.session.directory is None:
            raise RuntimeError("PDF session has not started")
        shell = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"] if os.name == "nt"
                 else ["/bin/sh", "-c"])
        command = arguments["command"]
        if os.name == "nt":
            command = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command
        try:
            code, text = await process(
                *shell, command, cwd=self.session.directory,
                env=command_environment(self.session.python_executable),
                timeout=self.session.command_timeout, limit=self.session.output_limit)
        except OutputLimitError as exc:
            # Decision H1: the worker sees the start and can retry with a narrower command,
            # instead of the whole attempt ending on one oversized output.
            limit = self.session.observation_chars or self.session.output_limit
            start = exc.partial.decode("utf-8", errors="replace")[:limit]
            return {"exit_code": None, "output": start + (
                f"\n\n[Command stopped: it printed more than {self.session.output_limit:,} bytes. Only the "
                f"start is shown. Save long output to a file and read parts of it with head, tail or grep.]")}
        return {"exit_code": code, "output": cut_output(text, self.session.observation_chars)}


class VenvPDFSession:
    """Copy only public inputs into a fresh folder. A venv is not a filesystem sandbox."""

    # Decision H3: an attempt cut off by its step or time budget, or by an error, is still scored
    # on the answer.json it left, as Trace2Skill and SkillOpt score whatever output file exists.
    collect_on_failure = True

    def __init__(self, *, pdf, schema, task, python_executable=None,
                 command_timeout=60, output_limit=128_000, observation_chars=OBSERVATION_CHARS):
        self.pdf, self.schema = Path(pdf), schema
        self.python_executable = python_path(python_executable)
        self.command_timeout, self.output_limit = command_timeout, output_limit
        # None shows whole outputs (up to output_limit); the inspector's shell bounds them itself.
        self.observation_chars = observation_chars
        self.temporary = self.directory = None
        self.tools = {"shell": ShellTool(self)}
        shell = "PowerShell" if os.name == "nt" else "POSIX sh"
        self.prompt = (task + "\nThe original PDF is inputs/document.pdf and the requested JSON schema "
                       "is inputs/schema.json, relative to the current working folder. "
                       f"Shell commands use {shell}; python uses the configured venv. "
                       "Inspect the PDF with shell tools. Write a UTF-8 JSON object to answer.json "
                       "in the current folder, then finish. Do not invent unavailable values.\n"
                       "Public output schema:\n" + json.dumps(schema, ensure_ascii=False))

    async def start(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="skill-agent-pdf-")
        self.directory = Path(self.temporary.name)
        try:
            inputs = self.directory / "inputs"
            inputs.mkdir()
            shutil.copyfile(self.pdf, inputs / "document.pdf")
            (inputs / "schema.json").write_text(json.dumps(self.schema), encoding="utf-8")
            return self
        except BaseException:
            await self.close()
            raise

    async def add_files(self, files):
        """Copy extra skill files ({name: file or folder}) into the working folder."""
        if self.directory is None:
            raise RuntimeError("PDF session has not started")
        for name, source in files.items():
            source, target = Path(source), self.directory / name
            if target.exists():
                raise ValueError(f"{name} already exists in the working folder")
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copyfile(source, target)

    async def collect(self):
        if self.directory is None:
            raise RuntimeError("PDF session has not started")
        try:
            with (self.directory / "answer.json").open("rb") as stream:
                raw = stream.read(1_000_001)
        except FileNotFoundError as exc:
            raise ValueError("no answer.json produced") from exc
        if len(raw) > 1_000_000:
            raise OutputLimitError("answer.json exceeds the output budget")
        result = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(result, dict) or "_error" in result:
            raise ValueError("answer.json must be a JSON object without reserved _error")
        return result

    async def close(self):
        if self.temporary:
            self.temporary.cleanup()
            self.temporary = self.directory = None
