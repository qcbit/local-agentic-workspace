import ast
import os
from pathlib import Path
import shlex
import subprocess
import sys


def get_python_interpreter() -> str:
    """Finds a valid Python interpreter, avoiding PyInstaller binary wrappers."""
    if not getattr(sys, "frozen", False):
        return sys.executable

    possible_interpreters = (
        ["python", "python3", "py"]
        if os.name == "nt"
        else ["python3", "/usr/bin/python3", "/usr/local/bin/python3"]
    )

    for interp in possible_interpreters:
        try:
            result = subprocess.run(
                [interp, "--version"], capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                return interp
        except Exception:
            continue

    return "python3"


def execute_python_repl(code: str, timeout: int = 5) -> str:
    """Executes Python code in a sandboxed child process with a strict timeout."""
    if not code:
        return "Error: No code provided."

    forbidden_modules = {"os", "sys", "subprocess", "shutil", "pty", "socket", "pathlib"}

    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden_modules:
                        return f"Error: Import of forbidden module '{alias.name}' is blocked by sandbox."
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] in forbidden_modules:
                    return f"Error: Import from forbidden module '{node.module}' is blocked by sandbox."
    except SyntaxError as e:
        return f"SyntaxError in provided code: {e}"

    python_bin = get_python_interpreter()

    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [python_bin, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=timeout,
        )

        output = result.stdout
        if result.stderr:
            output += f"\n--- STDERR ---\n{result.stderr}"

        return output.strip() if output.strip() else "Execution successful (no standard output). Did you forget to print()?"
    except subprocess.TimeoutExpired:
        return f"Error: Execution timed out after {timeout} seconds. Infinite loop prevented."
    except Exception as e:
        return f"Error executing python code: {str(e)}"


def validate_terminal_command(workspace_root: str, command_str: str) -> tuple[bool, str]:
    """Scans a shell command for path arguments escaping workspace bounds."""
    try:
        tokens = shlex.split(command_str)
    except Exception:
        return False, "Error: Invalid or unparseable shell command syntax."

    safe_root = Path(workspace_root).resolve(strict=True)

    for token in tokens:
        if token.startswith("/") or token.startswith("~") or ".." in token:
            expanded_token = os.path.expanduser(token)
            resolved_path = Path(expanded_token).resolve()
            try:
                resolved_path.relative_to(safe_root)
            except ValueError:
                return False, (
                    f"Error: Command blocked by sandbox. "
                    f"Target path '{token}' is outside authorized workspace root."
                )

    return True, ""


def is_path_safe(workspace_root: str, target_path: str) -> bool:
    """Strictly checks whether a path falls within the workspace root."""
    try:
        safe_root = Path(workspace_root).resolve(strict=True)
        target = Path(target_path).resolve()
        target.relative_to(safe_root)
        return True
    except (ValueError, RuntimeError):
        return False


def validate_python_syntax(code: str) -> str:
    """Checks Python code for syntax errors without executing."""
    if not code:
        return "Error: No code provided."
    try:
        ast.parse(code)
        return "✅ Syntax Check Passed: The provided Python code is syntactically valid."
    except SyntaxError as e:
        error_msg = f"❌ SyntaxError: {e.msg} at line {e.lineno}"
        if e.text:
            error_msg += f"\nCode snippet: {e.text.strip()}"
        return error_msg
    except Exception as e:
        return f"❌ Validation Error: {str(e)}"