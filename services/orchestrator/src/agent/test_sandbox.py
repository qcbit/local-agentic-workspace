import os
from pathlib import Path

# Paste the is_path_safe function here
def is_path_safe(workspace_root: str, target_path: str) -> bool:
    try:
        safe_root = Path(workspace_root).resolve(strict=True)
        target = Path(target_path).resolve()
        target.relative_to(safe_root)
        return True
    except (ValueError, RuntimeError):
        return False

# --- Tests ---
workspace = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

tests = [
    # 1. Standard authorized access
    ("Authorized read", os.path.join(workspace, "src", "index.ts"), True),
    
    # 2. Directory traversal attempt (malicious)
    ("Traversal outside", os.path.join(workspace, "..", "..", "etc", "passwd"), False),
    
    # 3. Sneaky traversal (going out and back in, but ultimately outside)
    ("Sneaky traversal", os.path.join(workspace, "src", "..", "..", "..", "Desktop"), False),
    
    # 4. Absolute path outside the workspace
    ("Absolute outside", "/Users/Shared/test.txt", False),
    
    # 5. Absolute path inside the workspace
    ("Absolute inside", os.path.join(workspace, "README.md"), True)
]

print(f"🔒 Workspace Root: {workspace}\n")
for name, path, expected in tests:
    result = is_path_safe(workspace, path)
    status = "✅ PASS" if result == expected else "❌ FAIL"
    print(f"{status} | {name}\n   Path: {path}\n   Expected: {expected} | Got: {result}\n")
