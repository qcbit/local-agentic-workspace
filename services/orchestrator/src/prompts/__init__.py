from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent

def load_prompt(name: str) -> str:
    """Load a prompt template text file from the prompts directory.
    
    Args:
        name: Filename without extension, e.g. 'agent_system_v1'
    
    Returns:
        The raw template text with format placeholders intact.
    """
    file_path = _PROMPTS_DIR / f"{name}.txt"
    if not file_path.exists():
        raise FileNotFoundError(f"Prompt template not found: {file_path}")
    return file_path.read_text(encoding="utf-8")
