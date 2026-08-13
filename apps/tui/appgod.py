import asyncio
import json
import logging
import os
import pyperclip
from services.orchestrator import Agent, OllamaProxyProvider
from textual import work, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Header, Footer, Input, Label, Markdown, RichLog, Switch
from typing import AsyncGenerator
import uuid

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

    #toggle-container {
        height: 3;
        align: left middle;
        padding-left: 1;
    }
    
    #toggle-label {
        margin-left: 1;
        content-align: center middle;
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
                with Horizontal(id="toggle-container"):
                    yield Switch(id="auto-approve-toggle")
                    yield Label("Autonomous Mode (Auto-Approve)", id="toggle-label")
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
        
        # Check the UI toggle state
        is_auto = self.query_one("#auto-approve-toggle", Switch).value

        # 1. Update application state
        self.chat_history.append({"role": "user", "content": user_text})
        
        # 2. Update UI state
        chat_scroll = self.query_one("#chat-scroll", VerticalScroll)
        chat_scroll.mount(Markdown(f"**You:** {user_text}", classes="message"))
        
        # Trigger the async orchestrator tasks
        self.process_agent_turn(user_text, is_auto)

    @work(exclusive=True)
    async def process_agent_turn(self, prompt: str, auto_approve: bool) -> None:
        chat_scroll = self.query_one("#chat-scroll", VerticalScroll)
        tool_log = self.query_one("#tool-log", RichLog)
        
        def ui_logger(message: str):
            tool_log.write(message)
            
        # 1. Mount the initial "Thinking" message in the UI
        agent_message = Markdown("**Agent:** Executing in VS Code context... ⏳", classes="message")
        await chat_scroll.mount(agent_message)
        chat_scroll.scroll_end(animate=False)
        
        socket_path = "/tmp/agent.sock"
        ui_logger(f"[dim]🔌 Connecting to VS Code Orchestrator at {socket_path}...[/dim]")
        
        try:
            # 2. Open the Unix Domain Socket connection
            reader, writer = await asyncio.open_unix_connection(socket_path)
            
            # 3. Format the JSON-RPC request to trigger the agent
            req_id = str(uuid.uuid4())
            payload = {
                "jsonrpc": "2.0",
                "method": "execute_agent_task",
                "params": {
                    "goal": prompt,
                    "auto_approve": auto_approve
                },
                "id": req_id
            }
            
            # 4. Send the task
            message = json.dumps(payload) + "\n"
            writer.write(message.encode('utf-8'))
            await writer.drain()
            
            ui_logger(f"[bold cyan]📤 Task dispatched![/bold cyan] Waiting for completion...")
            
            # 5. Wait for the server to finish the entire multi-turn ReAct loop
            response_bytes = await reader.readline()
            response_str = response_bytes.decode('utf-8').strip()
            
            if response_str:
                response_data = json.loads(response_str)
                ui_logger(f"\n📥 [bold green]Final Server Response:[/bold green]\n{json.dumps(response_data, indent=2)}")
                
                # Extract the final observation
                final_obs = "Task failed to return an observation."
                if "result" in response_data and "final_observation" in response_data["result"]:
                    final_obs = response_data["result"]["final_observation"]
                elif "error" in response_data:
                    final_obs = f"**Error:** {response_data['error'].get('message')}"
                    
                # Update the Chat UI with the final result
                await agent_message.update(f"**Agent:** {final_obs}")
                chat_scroll.scroll_end(animate=False)
                
            writer.close()
            await writer.wait_closed()
            
        except Exception as e:
            ui_logger(f"❌ [bold red]Connection failed: {e}[/bold red]\nMake sure uds_server.py is running.")
            await agent_message.update(f"**Agent:** Error connecting to VS Code backend. Is `uds_server.py` running?")

if __name__ == "__main__":
    app = AgentOrchestratorApp()
    app.run()
