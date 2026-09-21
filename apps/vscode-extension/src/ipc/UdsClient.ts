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

    constructor(port: number = 7777) {
        super();
        this.port = port;
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
                        const statusType = msg.params?.status || 'thinking';
        
                        if (statusType === 'reflecting') {
                            this.emit('agentReflecting', msg.params?.message);
                        } else {
                            this.emit('agentThinking', msg.params?.message);
                        }
                    }
                    continue;
                }

                // 1. REVERSE-REQUEST LOGIC: Is Python asking Node.js for data or permission?
                if (msg.method) {
                    (async () => {
                        let resultPayload: any = {};

                        try {
                            let activeEditor = vscode.window.activeTextEditor;
                            if (!activeEditor && vscode.window.visibleTextEditors.length > 0) {
                                activeEditor = vscode.window.visibleTextEditors.find(e => e.document.uri.scheme === 'file');
                            }

                            // --- TIER 1: Read-Only Context (Auto-Approve) ---
                            if (msg.method === "get_active_file_content") {
                                if (activeEditor) {
                                    resultPayload = { 
                                        content: `[File Path: ${activeEditor.document.uri.fsPath}]\n\n${activeEditor.document.getText()}` 
                                    };
                                } else {
                                    resultPayload = { content: "Error: No active or visible editor window found." };
                                }
                            } 

                            // --- LSP: Definition Lookup ---
                            else if (msg.method === "get_definition") {
                                const targetPath = msg.params?.uri || msg.params?.path;
                                const line = msg.params?.line ?? 0;
                                const character = msg.params?.character ?? 0;

                                if (!targetPath) {
                                    resultPayload = { error: "No URI provided for definition search." };
                                } else {
                                    const fileUri = vscode.Uri.file(targetPath);
                                    const position = new vscode.Position(line, character);
                                    
                                    const definitions: any = await vscode.commands.executeCommand(
                                        'vscode.executeDefinitionProvider',
                                        fileUri,
                                        position
                                    );

                                    resultPayload = (definitions || []).map((loc: any) => {
                                        const uri = loc.uri ? loc.uri.fsPath : loc.targetUri?.fsPath;
                                        const range = loc.range || loc.targetRange;
                                        return {
                                            uri: uri,
                                            line: range?.start?.line ?? 0,
                                            character: range?.start?.character ?? 0,
                                            end_line: range?.end?.line ?? 0,
                                            end_character: range?.end?.character ?? 0
                                        };
                                    });
                                }
                            }

                            // --- LSP: References Lookup ---
                            // --- LSP: References Lookup ---
                            else if (msg.method === "get_references") {
                                // 1. Strip the file:// protocol if the LLM hallucinates it
                                let targetPath = msg.params?.uri || msg.params?.path;
                                if (targetPath && targetPath.startsWith('file://')) {
                                    targetPath = targetPath.replace('file://', '');
                                }
                                
                                // 2. Ensure line numbers are 0-indexed. If the LLM sends a 1-indexed number 
                                // (e.g., matching what a human sees in the editor), we must subtract 1.
                                // We'll subtract 1 by default unless the LLM explicitly states it's 0-indexed.
                                // A safe fallback is to just subtract 1 if the line is > 0.
                                let line = msg.params?.line ?? 0;
                                if (line > 0) line -= 1; 
                                
                                const character = msg.params?.character ?? 0;

                                if (!targetPath) {
                                    resultPayload = { error: "No URI provided for references search." };
                                } else {
                                    const fileUri = vscode.Uri.file(targetPath);
                                    const position = new vscode.Position(line, character);
                                    
                                    const references = await vscode.commands.executeCommand<vscode.Location[]>(
                                        'vscode.executeReferenceProvider',
                                        fileUri,
                                        position
                                    );

                                    resultPayload = (references || []).map(loc => ({
                                        uri: loc.uri.fsPath,
                                        line: loc.range.start.line,
                                        character: loc.range.start.character,
                                        end_line: loc.range.end.line,
                                        end_character: loc.range.end.character
                                    }));
                                }
                            }

                            else if (msg.method === 'vscode_command') {
                                const command = msg.params?.command;
                                const targetPath = msg.params?.target_path || msg.params?.path || (msg.params?.args && msg.params?.args[0]); 

                                try {
                                    if (command === 'agenticWorkspace.getSecret' || command === 'agenticWorkspace.storeSecret') {
                                        const rawArg = msg.params?.target_path || msg.params?.key;
                                        const extraArg = msg.params?.value;
                                        const result: any = await vscode.commands.executeCommand(command, rawArg, extraArg);
                                        resultPayload = typeof result === 'object' && result !== null ? result : { value: result };
                                    }
                                    else if (command === 'vscode.openFolder') {
                                        if (!targetPath) {
                                            resultPayload = { content: `Error: No target path provided for ${command}.` };
                                        } else {
                                            const uri = vscode.Uri.file(targetPath);
                                            if (!fs.existsSync(uri.fsPath)) {
                                                resultPayload = { content: `Error: The directory '${targetPath}' does not exist on the file system.` };
                                            } else {
                                                setTimeout(() => {
                                                    vscode.commands.executeCommand(command, uri);
                                                }, 1000);
                                                resultPayload = { content: `Command accepted. VS Code is now reloading into ${targetPath}.` };
                                            }
                                        }
                                    } 
                                    else if (command === 'vscode.open') {
                                        if (!targetPath) {
                                            resultPayload = { content: `Error: No target path provided for ${command}.` };
                                        } else {
                                            const uri = vscode.Uri.file(targetPath);
                                            await vscode.commands.executeCommand(command, uri);
                                            resultPayload = { content: `Successfully executed '${command}' on '${targetPath}'.` };
                                        }
                                    } 
                                    else {
                                        const result: any = await vscode.commands.executeCommand(command, targetPath);
                                        resultPayload = typeof result === 'object' && result !== null ? result : { content: `Executed '${command}'`, value: result };
                                    }
                                } catch (err: any) {
                                    resultPayload = { content: `Failed to execute VS Code command '${command}': ${err.message}` };
                                }
                            }
                            
                            // --- TIER 3: Shell Commands (Explicit Modal) ---
                            else if (msg.method === 'request_shell_permission') {
                                const { command } = msg.params;
                                vscode.window.showInformationMessage("Agent is requesting terminal access. Please check the top of your window.");

                                const timeoutInput = await vscode.window.showInputBox({
                                    prompt: `Agent wants to run: ${command}. Enter timeout in seconds (Esc to deny).`,
                                    value: '30',
                                    ignoreFocusOut: true,
                                    validateInput: (text) => {
                                        const parsed = Number.parseInt(text, 10);
                                        if (Number.isNaN(parsed) || parsed <= 0) {
                                            return 'Timeout must be a valid positive number in seconds.';
                                        }
                                        return null;
                                    }
                                });

                                if (timeoutInput === undefined) {
                                    resultPayload = { status: 'denied', content: "Action Blocked: The user denied the shell execution request." };
                                } else {
                                    resultPayload = { status: 'approved', timeout: parseInt(timeoutInput, 10) };
                                }
                            }
                            
                            // --- TIER 2: File Writes (Staged Diff Approval) ---
                            else if (msg.method === "request_write_permission") {
                                const filePath = msg.params?.path;
                                const newContent = msg.params?.content;
                                
                                // 🎯 Force an explicit toaster popup so you know a write is pending
                                vscode.window.showInformationMessage(
                                    `Agent is proposing changes to ${path.basename(filePath)}. Please review the opened Diff window to Approve or Reject.`
                                );
                                
                                const response: any = await vscode.commands.executeCommand(
                                    'agenticWorkspace.handleWriteRequest', 
                                    filePath, 
                                    newContent
                                );
                                
                                resultPayload = response; 
                            }

                            // --- TIER 4: Silent Terminal Execution ---
                            else if (msg.method === "execute_terminal") {
                                const cmd = msg.params?.command;
                                
                                if (!cmd) {
                                    resultPayload = { output: "Error: No command provided." };
                                } else {
                                    console.log(`🤖 Agent executing command: ${cmd}`);
                                    const workspaceFolders = vscode.workspace.workspaceFolders;
                                    const activeWorkspace = workspaceFolders ? workspaceFolders[0].uri.fsPath : os.homedir();
                                    
                                    resultPayload = await new Promise((resolve) => {
                                        exec(cmd, { cwd: activeWorkspace }, (error, stdout, stderr) => {
                                            if (error) {
                                                resolve({ output: stderr || error.message });
                                            } else {
                                                resolve({ output: stdout.trim() });
                                            }
                                        });
                                    });
                                }
                            }

                            // Send response back to Python using Python's exact request ID
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
                    
                    continue;
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
