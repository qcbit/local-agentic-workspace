import * as fs from 'fs';
import * as net from 'net';
import * as os from 'os';
import * as path from 'path';
import * as vscode from 'vscode';

export class UdsClient {
    private client: net.Socket | null = null;
    private buffer: string = '';
    private pendingRequests: Map<number, { resolve: Function, reject: Function }> = new Map();
    private messageId: number = 0;
    private socketPath: string;

    constructor() {
        // Universally accessible location that both Node and Python can reach
        this.socketPath = '/tmp/agent.sock';
    }

    /**
     * Establishes the connection to the Unix Domain Socket.
     */
    public connect(): Promise<void> {
        return new Promise((resolve, reject) => {
            this.client = net.createConnection(this.socketPath);

            this.client.on('connect', () => {
                console.log(`🔌 Connected to Python Orchestrator at ${this.socketPath}`);
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
                            else if (msg.method === "get_selected_text") {
                                if (activeEditor && !activeEditor.selection.isEmpty) {
                                    resultPayload = { 
                                        content: `[File Path: ${activeEditor.document.uri.fsPath}]\n\n${activeEditor.document.getText(activeEditor.selection)}` 
                                    };
                                } else {
                                    resultPayload = { content: "Error: No text selected." };
                                }
                            }
                            
                            // --- TIER 3: Shell Commands (Explicit Modal) ---
                            else if (msg.method === "request_shell_permission") {
                                const command = msg.params?.command || "Unknown command";
                                
                                const choice = await vscode.window.showWarningMessage(
                                    `🚨 The AI Agent wants to execute a shell command:\n\n${command}\n\nDo you want to allow this?`,
                                    { modal: true },
                                    "Allow",
                                    "Deny"
                                );
                                
                                resultPayload = { status: choice === "Allow" ? "approved" : "denied" };
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
}
