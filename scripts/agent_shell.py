#!/usr/bin/env python3
import os
import sys
import subprocess
import socket
import json
import uuid
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.formatted_text import HTML

SOCKET_PATH = "/tmp/agent.sock"

def send_to_agent(user_prompt: str):
    """Sends a direct prompt to the background orchestrator via UDS and handles reverse-requests."""
    print("🤖 Agent is thinking...")
    
    req_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "execute_agent_task",
        "params": {"goal": user_prompt}
    }
    
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(SOCKET_PATH)
            
            # Send the initial prompt
            client.sendall((json.dumps(payload) + '\n').encode('utf-8'))
            
            buffer = ""
            while True:
                # Read chunks from the socket
                data = client.recv(4096)
                if not data:
                    break
                
                buffer += data.decode('utf-8')
                
                # Process complete JSON lines
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    if not line.strip():
                        continue
                        
                    msg = json.loads(line)
                    
                    # 1. Is this a reverse-request from the server? (It has a method and an id)
                    if "method" in msg and "id" in msg:
                        method = msg["method"]
                        params = msg.get("params", {})
                        
                        # Terminal-native security proxy
                        print(f"\n⚠️  [Agent wants to execute a tool via '{method}']")
                        print(f"   Params: {json.dumps(params, indent=2)}")
                        choice = input("   Approve? (y/n): ")
                        
                        # Route the approval back to the waiting orchestrator
                        resp = {
                            "jsonrpc": "2.0",
                            "id": msg["id"],
                            "result": {"status": "approved" if choice.lower() == 'y' else "denied"}
                        }
                        client.sendall((json.dumps(resp) + '\n').encode('utf-8'))
                        print("🤖 Agent resuming...")
                        continue
                        
                    # 2. Is this the final result matching our original request ID?
                    if "result" in msg and msg.get("id") == req_id:
                        print(f"\n✅ Agent Finished:\n{msg['result'].get('final_observation')}\n")
                        return
                        
                    # 3. Did the agent loop crash?
                    if "error" in msg and msg.get("id") == req_id:
                        print(f"\n❌ Error: {msg.get('error')}\n")
                        return
                        
    except FileNotFoundError:
        print(f"\n❌ Error: Cannot find socket at {SOCKET_PATH}. Is uds_server.py running?\n")
    except Exception as e:
        print(f"\n❌ Connection failed: {e}\n")

def execute_shell_command(cmd: str):
    """Executes a native shell command and intercepts failures."""
    try:
        # Run the command and capture the output so we can feed it to the LLM if it fails
        result = subprocess.run(
            cmd, 
            shell=True, 
            text=True, 
            capture_output=True
        )
        
        # Print the output exactly as it would appear in a normal terminal
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
            
        # WARP-STYLE INTERCEPT: If the command failed, offer to fix it
        if result.returncode != 0:
            print(f"\n🚨 [Exit Code {result.returncode}] Command failed.")
            user_choice = input("Press [Enter] to ask the Agent to fix this, or type 'n' to skip: ")
            
            if user_choice.strip().lower() != 'n':
                error_goal = (
                    f"I ran the command `{cmd}` and it failed with exit code {result.returncode}.\n"
                    f"Here is the error output:\n```\n{result.stderr}\n```\n"
                    f"Please analyze this, explain the issue, and use the terminal to fix it."
                )
                send_to_agent(error_goal)

    except KeyboardInterrupt:
        print("\n^C")
    except Exception as e:
        print(f"Shell execution error: {e}")

def main():
    print("🚀 Welcome to the Agent Shell. Type normal commands, or use '@agent' to talk to the AI.")
    history = InMemoryHistory()

    while True:
        try:
            # Display a custom prompt
            user_input = prompt(
                HTML('<b><ansigreen>(agent-shell)</ansigreen> $ </b>'), 
                history=history
            ).strip()

            if not user_input:
                continue

            if user_input.lower() in ['exit', 'quit']:
                break

            # Check for the wake word
            if user_input.startswith("@agent"):
                # Strip the wake word and send the rest to the AI
                actual_prompt = user_input[len("@agent"):].strip()
                send_to_agent(actual_prompt)
            else:
                # Treat as a normal shell command
                execute_shell_command(user_input)

        except KeyboardInterrupt:
            # Handle Ctrl+C gracefully
            continue
        except EOFError:
            # Handle Ctrl+D
            break

if __name__ == "__main__":
    main()
