import asyncio
from textual import work, on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Footer, Input, Markdown, RichLog
from typing import AsyncGenerator

# ------------------------------------------------------------------
# Backend Mocks (Replace these with your actual orchestrator logic)
# ------------------------------------------------------------------

async def actual_llm_stream(messages: list[dict[str, str]]) -> AsyncGenerator[str, None]:
    """Replace with your actual LLM call using the message history."""
    
    # For the mock response, we'll just grab the user's latest prompt
    latest_prompt = messages[-1]["content"]
    
    response = f"I've processed the request for '{latest_prompt}'." 
    
    for word in response.split():
        await asyncio.sleep(0.05)
        yield word + " "

async def actual_tool_execution(prompt: str, log_widget: RichLog):
    """Replace with your actual tool registry hooks."""
    log_widget.write(f"[bold yellow]> Executing loop for:[/bold yellow] {prompt}")
    await asyncio.sleep(0.5)
    
    if "math" in prompt.lower():
        log_widget.write("└─ [dim]Querying tool registries...[/dim]")
        await asyncio.sleep(0.3)
        log_widget.write("└─ [bold cyan]Loading Native math_operation Tool...[/bold cyan]")
        await asyncio.sleep(0.5)
        log_widget.write("└─ [green]Execution complete. Result: 42[/green]")
    elif "fix" in prompt.lower():
        log_widget.write("└─ [dim]Analyzing terminal history...[/dim]")
        await asyncio.sleep(0.3)
        log_widget.write("└─ [bold cyan]Initiating Auto-fix broken command...[/bold cyan]")
        await asyncio.sleep(0.5)
        log_widget.write("└─ [green]Command patched successfully.[/green]")
    else:
        log_widget.write("└─ [dim]No specific tool triggered. Standard response.[/dim]")

# ------------------------------------------------------------------
# UI Application
# ------------------------------------------------------------------

class AgentOrchestratorApp(App):
    """A split-pane streaming TUI for an AI Agent."""
    
    CSS = """
    Screen {
        layout: horizontal;
    }
    
    #chat-pane {
        width: 60%;
        height: 100%;
        border-right: solid green;
    }
    
    #tool-pane {
        width: 40%;
        height: 100%;
        background: $panel;
    }
    
    #chat-scroll {
        height: 1fr;
        padding: 1;
    }
    
    #tool-log {
        height: 100%;
        padding: 1;
    }
    
    Input {
        dock: bottom;
    }
    
    .message {
        margin-bottom: 1;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Initialize your application state here
        self.chat_history = [
            {"role": "system", "content": "You are a helpful AI agent orchestrator."}
        ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        
        with Horizontal():
            # Left side: Streaming Conversation
            with Vertical(id="chat-pane"):
                yield VerticalScroll(id="chat-scroll")
                yield Input(placeholder="Ask the agent to run a math_operation or auto-fix a command...", id="chat-input")
            
            # Right side: Tool Logs
            with Vertical(id="tool-pane"):
                yield RichLog(id="tool-log", highlight=True, wrap=True, markup=True)
                
        yield Footer()

    def on_mount(self) -> None:
        """Initialize the UI state."""
        self.query_one("#tool-log", RichLog).write("[bold magenta]System:[/bold magenta] Orchestrator registries loaded. Ready.")

    @on(Input.Submitted, "#chat-input")
    def handle_submit(self, event: Input.Submitted) -> None:
        user_text = event.value
        if not user_text.strip():
            return
            
        event.input.value = "" 
        
        # 1. Update application state
        self.chat_history.append({"role": "user", "content": user_text})
        
        # 2. Update UI state
        chat_scroll = self.query_one("#chat-scroll", VerticalScroll)
        chat_scroll.mount(Markdown(f"**You:** {user_text}", classes="message"))
        
        # Trigger the async orchestrator tasks
        self.process_agent_turn()

    @work(exclusive=True)
    async def process_agent_turn(self, prompt: str) -> None:
        """Handles both the LLM stream and the tool execution side-by-side."""
        chat_scroll = self.query_one("#chat-scroll", VerticalScroll)
        tool_log = self.query_one("#tool-log", RichLog)
        
        # 1. Fire off the tool execution to the right pane
        # Grab the latest prompt for the tool execution mock
        latest_prompt = self.chat_history[-1]["content"]

        # We use asyncio.create_task so it runs concurrently with the LLM stream
        asyncio.create_task(actual_tool_execution(latest_prompt, tool_log))
        
        # 2. Prep the UI for the agent's text response on the left pane
        agent_message = Markdown("**Agent:** ", classes="message")
        await chat_scroll.mount(agent_message)
        chat_scroll.scroll_end(animate=False)
        
        # Keep the raw response separate from the UI formatting
        raw_response: str = ""
        
        # 3. Stream the LLM tokens
        # Pass the ENTIRE history list to your LLM stream
        async for token in actual_llm_stream(self.chat_history):
            raw_response += token
            # Prefix the UI update with the bold Agent tag
            agent_message.update(f"**Agent:** {raw_response}")
            chat_scroll.scroll_end(animate=False)
            
        # Finally, save the agent's completed response to the state
        self.chat_history.append({"role": "assistant", "content": raw_response})

if __name__ == "__main__":
    app = AgentOrchestratorApp()
    app.run()
