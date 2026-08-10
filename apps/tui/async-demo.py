import asyncio
from textual import work, on
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Input, Markdown

# 1. Mock Async LLM Generator
# In reality, this would be your async OpenAI/Anthropic/Local LLM stream
async def mock_llm_stream(prompt: str):
    response_chunks = [
        "Here ", "is ", "the ", "streamed ", "response ", 
        "from ", "the ", "orchestrator loop. ", "Notice ", 
        "how ", "the ", "UI ", "doesn't ", "freeze!"
    ]
    for chunk in response_chunks:
        await asyncio.sleep(0.1)  # Simulate network latency and token generation
        yield chunk

class AsyncStreamingApp(App):
    CSS = """
    #chat-container {
        height: 1fr;
        border: solid green;
        padding: 1;
    }
    Input {
        dock: bottom;
    }
    .message {
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        # Replaced RichLog with VerticalScroll for dynamic widget updates
        yield VerticalScroll(id="chat-container")
        yield Input(placeholder="Type a message...", id="chat-input")

    @on(Input.Submitted, "#chat-input")
    def handle_submit(self, event: Input.Submitted) -> None:
        user_text = event.value
        if not user_text.strip():
            return
            
        # Clear input immediately
        event.input.value = ""
        
        # Add user message to UI
        container = self.query_one("#chat-container", VerticalScroll)
        container.mount(Markdown(f"**You:** {user_text}", classes="message"))
        
        # Trigger the async background task
        self.stream_agent_response(user_text)

    # 2. The @work decorator
    # exclusive=True prevents the user from triggering multiple streams at once
    @work(exclusive=True)
    async def stream_agent_response(self, prompt: str) -> None:
        container = self.query_one("#chat-container", VerticalScroll)
        
        # Create an empty Markdown widget for the agent's response and mount it
        agent_message = Markdown("**Agent:** ", classes="message")
        await container.mount(agent_message)
        
        # Automatically scroll to the bottom so the new message is visible
        container.scroll_end(animate=False)
        
        # Accumulate the stream
        full_response = "**Agent:** "
        
        # Iterate over your async LLM generator
        async for token in mock_llm_stream(prompt):
            full_response += token
            # 3. Update the widget dynamically
            # Textual handles this safely from within the @work task
            agent_message.update(full_response)
            
            # Keep scrolling down if the message gets long
            container.scroll_end(animate=False)

if __name__ == "__main__":
    app = AsyncStreamingApp()
    app.run()
