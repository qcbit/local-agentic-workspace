import { EventEmitter } from 'events';
import { exec } from 'child_process';
import * as fs from 'fs';
import * as net from 'net';
import * as os from 'os';
import * as path from 'path';
import * as vscode from 'vscode';

export class UdsClient extends EventEmitter {
    private client: net.Socket | null = null;
    private buffer: string = '';
    private pendingRequests: Map<number, { resolve: Function, reject: Function }> = new Map();
    private messageId: number = 0;
    private port: number = 7777;
    private host: string = '127.0.0.1';

    constructor() {
        super();
    }

    /**
     * Establishes the connection to the TCP Socket.
     */
    public connect(): Promise<void> {
        return new Promise((resolve, reject) => {
            this.client = net.createConnection({ port: this.port, host: this.host });

            this.client.on('connect', () => {
                console.log(`🔌 Connected to Python Orchestrator at ${this.host}:${this.port}`);
                resolve();
            });

            this.client.on('data', (data) => {
                this.buffer += data.toString();
                this.processBuffer();
            });

            this.client.on('error', (err) => {
                console.error('❌ UDS Client Error:', err.message);
                reject(err);
            });

            this.client.on('close', () => {
                console.log('⚠️ UDS Connection closed.');
                this.client = null;
            });
        });
    }

    /**
     * Processes the incoming data stream, splitting by newline to handle 
     * complete JSON-RPC payloads as they arrive.
     */
    private processBuffer() {
        let newlineIndex;
        while ((newlineIndex = this.buffer.indexOf('\n')) !== -1) {
            const message = this.buffer.slice(0, newlineIndex);
            this.buffer = this.buffer.slice(newlineIndex + 1);
            
            try {
                const msg = JSON.parse(message);
                
                if (msg.method && msg.id === undefined) {
                    if (msg.method === "agent_status") {
                        // Broadcast the agent's thought to the rest of the extension
                        this.emit('agentThinking', msg.params?.message);
                    }
                    continue; // Skip the reverse-request logic
                }

                // 1. REVERSE-REQUEST LOGIC: Is Python asking Node.js for data or permission?
                if (msg.method) {
                    // Handle the request asynchronously so we don't block the socket stream
                    (async () => {
                        let resultPayload: any = {};

                        try {
                            // Capture the active editor, or fallback to the first visible file editor if the chat panel stole focus
                            let activeEditor = vscode.window.activeTextEditor;
                            if (!activeEditor && vscode.window.visibleTextEditors.length > 0) {
                                activeEditor = vscode.window.visibleTextEditors.find(e => e.document.uri.scheme === 'file');
                            }

                            // --- TIER 1: Read-Only Context (Auto-Approve) ---
                            if (msg.method === "get_active_file_content") {
                                if (activeEditor) {
                                    // Inject the exact file path at the top so the LLM knows where to write!
                                    resultPayload = { 
                                        content: `[File Path: ${activeEditor.document.uri.fsPath}]\n\n${activeEditor.document.getText()}` 
                                    };
                                } else {
                                    resultPayload = { content: "Error: No active or visible editor window found." };
                                }
                            } 

                            else if (msg.method === 'vscode_command') {
                                const command = msg.params.command;
                                const targetPath = msg.params.target_path || msg.params.path || (msg.params.args && msg.params.args[0]); 

                                if (!targetPath || typeof targetPath !== 'string' || targetPath.trim() === '') {
                                    resultPayload = { content: `Error: No valid target path provided. Received: ${JSON.stringify(msg.params)}` };
                                } else {
                                    try {
                                        const uri = vscode.Uri.file(targetPath);
                                        // The Window Reload Trap Fix
                                        if (command === 'vscode.openFolder') {
                                            // 🎯 Verify the directory actually exists!
                                            if (!fs.existsSync(uri.fsPath)) {
                                                resultPayload = { content: `Error: The directory '${targetPath}' does not exist on the file system.` };
                                            } else {
                                                setTimeout(() => {
                                                    vscode.commands.executeCommand(command, uri);
                                                }, 1000);
                                                
                                                resultPayload = { content: `Command accepted. VS Code is now reloading into ${targetPath}.` };
                                            }
                                        } else {
                                            // Standard file opens
                                            await vscode.commands.executeCommand(command, uri);
                                            resultPayload = { content: `Successfully executed '${command}' on '${targetPath}'.` };
                                        }
                                        
                                    } catch (err: any) {
                                        resultPayload = { content: `Failed to execute VS Code command: ${err.message}` };
                                    }
                                }
                            }
                            
                            // --- TIER 3: Shell Commands (Explicit Modal) ---
                            else if (msg.method === 'request_shell_permission') {
                                const { command } = msg.params;
                                
                                // 🎯 Draw the user's attention!
                                vscode.window.showInformationMessage("Agent is requesting terminal access. Please check the top of your window.");

                                // Prompt the user with an input box instead of simple buttons
                                const timeoutInput = await vscode.window.showInputBox({
                                    prompt: `Agent wants to run: ${command}. Enter timeout in seconds (Esc to deny).`,
                                    value: '30', // Default to 30 seconds
                                    ignoreFocusOut: true, // 🎯 Keeps the input box visible even if you click away!
                                    validateInput: (text) => {
                                        const parsed = Number.parseInt(text, 10);
                                        if (Number.isNaN(parsed) || parsed <= 0) {
                                            return 'Timeout must be a valid positive number in seconds.';
                                        }
                                        return null; // Valid
                                    }
                                });

                                // 2. Route the user's decision back to Python by assigning to resultPayload
                                if (timeoutInput === undefined) {
                                    // If the user presses Escape, timeoutInput is undefined
                                    resultPayload = { status: 'denied', content: "Action Blocked: The user denied the shell execution request." };
                                } else {
                                    resultPayload = { status: 'approved', timeout: parseInt(timeoutInput, 10) };
                                }
                            }
                            
                            // --- TIER 2: File Writes (Staged Diff Approval) ---
                            else if (msg.method === "request_write_permission") {
                                const filePath = msg.params?.path;
                                const newContent = msg.params?.content;
                                
                                // Pause the IPC stream and delegate the UI to extension.ts
                                const response: any = await vscode.commands.executeCommand(
                                    'agenticWorkspace.handleWriteRequest', 
                                    filePath, 
                                    newContent
                                );
                                
                                // The command will return { status: "approved" | "denied" }
                                resultPayload = response; 
                            }

                            // --- TIER 4: Silent Terminal Execution ---
                            else if (msg.method === "execute_terminal") {
                                const cmd = msg.params?.command;
                                
                                if (!cmd) {
                                    resultPayload = { output: "Error: No command provided." };
                                } else {
                                    console.log(`🤖 Agent executing command: ${cmd}`);
                                    
                                    // Calculate the active workspace for the command's current working directory
                                    const workspaceFolders = vscode.workspace.workspaceFolders;
                                    const activeWorkspace = workspaceFolders ? workspaceFolders[0].uri.fsPath : os.homedir();
                                    
                                    // Wrap exec in a Promise to pause the async loop until the command finishes
                                    resultPayload = await new Promise((resolve) => {
                                        exec(cmd, { cwd: activeWorkspace }, (error, stdout, stderr) => {
                                            if (error) {
                                                // Send the error back so the agent can self-correct!
                                                resolve({ output: stderr || error.message });
                                            } else {
                                                // Send the successful standard output back to the agent
                                                resolve({ output: stdout.trim() });
                                            }
                                        });
                                    });
                                }
                            }

                            // Fire the response back to Python using Python's exact ID
                            if (this.client) {
                                const response = {
                                    jsonrpc: "2.0",
                                    id: msg.id,
                                    result: resultPayload
                                };
                                this.client.write(JSON.stringify(response) + '\n');
                            }

                        } catch (error: any) {
                            if (this.client) {
                                const errorResponse = {
                                    jsonrpc: "2.0",
                                    id: msg.id,
                                    error: { code: -32000, message: error.message }
                                };
                                this.client.write(JSON.stringify(errorResponse) + '\n');
                            }
                        }
                    })();
                    
                    continue; // Skip to the next message in the buffer
                }

                // 2. EXISTING LOGIC: Handle standard responses to our Node.js requests
                if (msg.id !== undefined && this.pendingRequests.has(msg.id)) {
                    const { resolve, reject } = this.pendingRequests.get(msg.id)!;
                    
                    if (msg.error) {
                        reject(msg.error);
                    } else {
                        resolve(msg.result);
                    }
                    
                    this.pendingRequests.delete(msg.id);
                }
            } catch (e) {
                console.error('Failed to parse JSON-RPC message:', e);
            }
        }
    }

    /**
     * Sends a formatted JSON-RPC request and returns a Promise that resolves 
     * when the server responds.
     */
    public request(method: string, params: any = {}): Promise<any> {
        return new Promise((resolve, reject) => {
            if (!this.client) {
                return reject(new Error("Client is not connected to the orchestrator."));
            }

            // Proactively inject the active file path into the user's goal
            // Fixed the problem with agent failing to invoke the get_active_file_content
            if (method === 'execute_agent_task' && params.goal) {
                let activeEditor = vscode.window.activeTextEditor;
                if (!activeEditor && vscode.window.visibleTextEditors.length > 0) {
                    activeEditor = vscode.window.visibleTextEditors.find(e => e.document.uri.scheme === 'file');
                }
                
                if (activeEditor) {
                    const filePath = activeEditor.document.uri.fsPath;
                    params.goal = `${params.goal}\n\n[System Context: The user's active file is currently ${filePath}]`;
                }
            }

            this.messageId++;
            const id = this.messageId;
            
            // Store the Promise handlers so we can resolve them when the data stream catches up
            this.pendingRequests.set(id, { resolve, reject });

            const payload = {
                jsonrpc: "2.0",
                method,
                params,
                id
            };

            this.client.write(JSON.stringify(payload) + '\n');
        });
    }

    /**
     * Sends a spontaneous JSON-RPC notification to the Python server.
     * Unlike a request, a notification does not expect a response, so it has no ID.
     */
    public sendNotification(method: string, params: any): void {
        if (!this.client) {
            console.warn("Cannot send notification: IPC client is not connected.");
            return;
        }

        const notification = {
            jsonrpc: "2.0",
            method: method,
            params: params
        };
        
        this.client.write(JSON.stringify(notification) + '\n');
        console.log(`📡 [IPC] Pushed notification: ${method}`);
    }

    /**
     * 🛑 Emergency Brake: Sends an instant notification to kill the running agent loop.
     */
    public cancelTask(): void {
        this.sendNotification("cancel_agent_task", {});
    }
}
