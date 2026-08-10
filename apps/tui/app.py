import asyncio
import logging
import pyperclip
from services.orchestrator import Agent, OllamaProxyProvider
from textual import work, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Header, Footer, Input, Label, Markdown, RichLog
from typing import AsyncGenerator

# Set up file logging
logging.basicConfig(
    filename='agent_tui.log',
    filemode='a', # Append mode
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

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

# 2. Define the Modal Popup Screen
class PermissionScreen(ModalScreen[bool]):
    """A modal popup to confirm dangerous agent actions."""
    
    CSS = """
    PermissionScreen {
        align: center middle;
        background: $background 80%; /* Dims the background */
    }
    #dialog {
        grid-size: 2;
        grid-gutter: 1 2;
        grid-rows: 1fr 3;
        padding: 1;
        width: 60;
        height: 11;
        border: thick $accent;
        background: $surface;
    }
    #question {
        column-span: 2;
        height: 1fr;
        width: 1fr;
        content-align: center middle;
        text-style: bold;
    }
    Button {
        width: 100%;
    }
    """
    
    def __init__(self, message: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.message = message

    def compose(self) -> ComposeResult:
        # Build the grid with a text label and two buttons
        yield Grid(
            Label(self.message, id="question"),
            Button("Deny", variant="error", id="deny"),
            Button("Approve", variant="success", id="approve"),
            id="dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # dismiss() returns the True/False value back to whoever called push_screen_wait()
        if event.button.id == "approve":
            self.dismiss(True)
        else:
            self.dismiss(False)

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

    # 1. Bind a key (e.g., Ctrl+y for 'yank' or copy)
    BINDINGS = [
        Binding("ctrl+y", "copy_last_message", "Copy Last Message")
    ]

    # 2. Define the action that runs when the key is pressed
    def action_copy_last_message(self) -> None:
        """Copies the most recent agent summary to the clipboard."""
        if len(self.chat_history) > 1:
            # Grab the last item in the history list
            last_message = self.chat_history[-1]
            
            if last_message["role"] == "assistant":
                pyperclip.copy(last_message["content"])
                
                # Optionally flash a notification on the screen
                self.notify("Agent response copied to clipboard!", timeout=2)

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
        self.process_agent_turn(user_text)

    @work(exclusive=True)
    async def process_agent_turn(self, prompt: str) -> None:
        """Handles the agent execution loop and streams logs to the UI."""
        chat_scroll = self.query_one("#chat-scroll", VerticalScroll)
        tool_log = self.query_one("#tool-log", RichLog)
        
        # 1. Define the callback that connects the Agent to the RichLog pane
        def ui_logger(message: str):
            tool_log.write(message)               # Writes to the UI
            logging.info(f"TOOL LOG: {message}")  # Writes to agent_tui.log

        # Define the async permission callback for the Agent
        async def ask_permission(message: str) -> bool:
            # This pauses the agent's background worker thread until the user clicks a button
            return await self.push_screen_wait(PermissionScreen(message))
            
        # 2. Setup the UI for the Agent's turn
        agent_message = Markdown("**Agent:** Thinking... ⏳", classes="message")
        await chat_scroll.mount(agent_message)
        chat_scroll.scroll_end(animate=False)
        
        # 3. Initialize your real backend
        llm = OllamaProxyProvider(
            # endpoint_url="http://localhost:11434/v1/chat/completions",
        )
        agent = Agent(
            llm_provider=llm, 
            config={},
            permission_callback=ask_permission
        )
        
        try:
            # 4. Await the execution loop, passing in the ui_logger
            final_state = await agent.run(prompt, ui_callback=ui_logger)
            
            # 5. When the loop hits 'finish_task', update the chat pane with the final summary
            agent_message.update(f"**Agent:** {final_state.summary}")
            
            # 6. Save the state to history
            self.chat_history.append({"role": "assistant", "content": final_state.summary})
            
        except Exception as e:
            agent_message.update(f"**Agent [Error]:** {str(e)}")
            tool_log.write(f"[bold red]System Error:[/bold red] {str(e)}")
            
        finally:
            chat_scroll.scroll_end(animate=False)

if __name__ == "__main__":
    app = AgentOrchestratorApp()
    app.run()
