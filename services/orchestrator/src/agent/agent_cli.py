#!/usr/bin/env python3
import socket
import json
import sys
import uuid

def send_to_agent(prompt: str):
    socket_path = "/tmp/agent.sock"
    req_id = str(uuid.uuid4())
    
    # Construct the exact same JSON-RPC payload Node.js sends
    payload = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "execute_agent_task",
        "params": {"goal": prompt}
    }
    
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall((json.dumps(payload) + '\n').encode('utf-8'))
            
            buffer = ""
            
            # Loop continuously to catch all streaming messages from the socket
            while True:
                data = client.recv(4096)
                if not data:
                    break
                    
                buffer += data.decode('utf-8')
                
                # Process potentially multiple JSON messages separated by newlines
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    if not line.strip():
                        continue
                        
                    message = json.loads(line)
                    
                    # 1. Handle live status updates from the orchestrator
                    if "method" in message and message["method"] == "agent_status":
                        print(message["params"].get("message", ""))
                        continue
                        
                    # 2. Handle the final JSON-RPC response matching our request ID
                    if message.get("id") == req_id:
                        if "result" in message:
                            res_data = message["result"]
                            
                            # Safely extract the summary depending on how the backend structures it
                            if isinstance(res_data, dict):
                                summary = res_data.get("summary", res_data)
                            else:
                                summary = res_data
                                
                            print(f"\n✅ Agent Finished:\n{summary}")
                        else:
                            print(f"\n❌ Error:\n{message.get('error', 'Unknown Error')}")
                            
                        # Exit the script once the final answer is received
                        return 
                        
    except Exception as e:
        print(f"Failed to connect to agent: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Join all command line arguments into a single prompt string
        user_prompt = " ".join(sys.argv[1:])
        send_to_agent(user_prompt)
    else:
        print("Usage: agent <your prompt>")
