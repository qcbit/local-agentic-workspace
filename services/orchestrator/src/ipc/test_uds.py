import asyncio
import json

async def test_ping():
    socket_path = "/tmp/agent.sock"
    print(f"🔌 Connecting to {socket_path}...")
    
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        
        # Format the JSON-RPC request
        payload = {"jsonrpc": "2.0", "method": "ping", "id": 99}
        message = json.dumps(payload) + "\n"
        
        print(f"📤 Sending: {message.strip()}")
        writer.write(message.encode('utf-8'))
        await writer.drain()
        
        # Read the response
        response = await reader.readline()
        print(f"📥 Received: {response.decode('utf-8').strip()}")
        
        writer.close()
        await writer.wait_closed()
        
    except Exception as e:
        print(f"❌ Connection failed: {e}")

async def test_agent_execution():
    socket_path = "/tmp/agent.sock"
    print(f"🔌 Connecting to {socket_path}...")
    
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        
        # Trigger the newly integrated Agent loop
        payload = {
            "jsonrpc": "2.0", 
            "method": "execute_agent_task", 
            "params": {"goal": "List the files in my current directory."},
            "id": 100
        }
        
        message = json.dumps(payload) + "\n"
        print(f"📤 Sending: {message.strip()}")
        
        writer.write(message.encode('utf-8'))
        await writer.drain()
        
        # Wait for the agent to finish its multi-turn loop and return the result
        print("⏳ Waiting for Agent loop to complete...")
        response = await reader.readline()
        
        # Pretty print the final JSON-RPC response
        parsed_response = json.loads(response.decode('utf-8').strip())
        print("\n📥 Final Response from Server:")
        print(json.dumps(parsed_response, indent=2))
        
        writer.close()
        await writer.wait_closed()
        
    except Exception as e:
        print(f"❌ Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_agent_execution())
