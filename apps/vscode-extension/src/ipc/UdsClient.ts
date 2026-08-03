import * as net from 'net';
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
                
                // 1. REVERSE-REQUEST LOGIC: Is Python asking Node.js for data?
                if (msg.method) {
                    let resultContent = "";
                    try {
                        const activeEditor = vscode.window.activeTextEditor;
                        
                        if (msg.method === "get_active_file_content") {
                            if (activeEditor) {
                                resultContent = activeEditor.document.getText();
                            } else {
                                resultContent = "No active editor window found.";
                            }
                        } else if (msg.method === "get_selected_text") {
                            if (activeEditor && !activeEditor.selection.isEmpty) {
                                resultContent = activeEditor.document.getText(activeEditor.selection);
                            } else {
                                resultContent = "No text selected.";
                            }
                        }

                        // Send the result back to Python using Python's exact ID
                        if (this.client) {
                            const response = {
                                jsonrpc: "2.0",
                                id: msg.id, 
                                result: { content: resultContent }
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
                    continue; // Skip the rest of the loop, we handled the request
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
}
