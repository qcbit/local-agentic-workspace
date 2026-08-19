import * as fs from 'fs';
import * as http from 'http';
import * as os from 'os';
import * as path from 'path';
import * as vscode from 'vscode';
import { ASTProvider } from './ast/ASTProvider';
import { AgentApprovalCodeLensProvider } from './codelens';
import { AgenticDiffProvider } from './providers/DiffProvider';
import { ChatViewProvider } from './providers/ChatViewProvider';
import { spawn, ChildProcess } from 'child_process';
import { UdsClient } from './ipc/UdsClient';
import { WarpProxyServer } from './proxy/WarpProxyServer';

let udsClient: UdsClient;
let proxyServer: WarpProxyServer;
let statusBarItem: vscode.StatusBarItem;
let backendProcess: ChildProcess | undefined;
const codeLensProvider = new AgentApprovalCodeLensProvider();

export function activate(context: vscode.ExtensionContext) {
    // 1. Move the workspace definition to the very top so it can be used immediately
    const workspaceFolders = vscode.workspace.workspaceFolders;
    const activeWorkspace = workspaceFolders ? workspaceFolders[0].uri.fsPath : os.homedir();

    // 2. Read the user-defined path from VS Code settings
    const config = vscode.workspace.getConfiguration('local-agentic-workspace');
    const globalConfigPath = config.get<string>('globalConfigPath', '');

    let command: string;
    let args: string[] = [];

    // 3. Check if we are running via F5 (Debug Mode) or in production
    if (context.extensionMode === vscode.ExtensionMode.Development) {
        // 🐛 DEBUG MODE: Run the raw Python script directly!
        command = '/Users/lance/Developer/local-agentic-workspace/.venv/bin/python3';
        const scriptPath = path.join(context.extensionPath, '../../services/orchestrator/src/ipc/uds_server.py');
        args = [scriptPath];
        vscode.window.showInformationMessage("🐛 Starting Agentic Backend in Developer Mode");
    } else {
        // 🚀 PRODUCTION MODE: Run the compiled PyInstaller binary
        const platform = os.platform();
        const arch = os.arch();

        let binaryName = 'uds_server-linux-x64'; // Fallback
        if (platform === 'darwin' && arch === 'arm64') {
            binaryName = 'uds_server-macos-arm64';
        } else if (platform === 'darwin' && arch === 'x64') {
            binaryName = 'uds_server-macos-x64';
        } else if (platform === 'win32') {
            binaryName = 'uds_server-win-x64.exe';
        }
        
        command = path.join(context.extensionPath, 'bin', binaryName);
    }

    // 4. Inject Dynamic CLI Arguments
    // 🎯 Force pass the true workspace as a CLI argument!
    args.push("--workspace", activeWorkspace);

    // 🎯 Pass the global config path if the user defined one
    if (globalConfigPath && globalConfigPath.trim() !== '') {
        args.push('--global-config', globalConfigPath.trim());
    }

    const backendChannel = vscode.window.createOutputChannel('Local Agentic Backend');
    context.subscriptions.push(backendChannel);

    // 5. Failsafe: Nuke the orphaned socket if it exists from a previous run
    const socketPath = '/tmp/agent.sock';
    if (fs.existsSync(socketPath)) {
        console.log('🧹 Cleaning up orphaned UDS socket...');
        fs.unlinkSync(socketPath);
    }

    backendChannel.appendLine(`🚀 Spawning backend using: ${command}`);

    // 6. Spawn the backend ONCE using the dynamically selected command
    backendProcess = spawn(command, args, {
        cwd: activeWorkspace,
        env: { 
            ...process.env, 
            PYTHONUNBUFFERED: "1",
            AGENTIC_WORKSPACE_ROOT: activeWorkspace // Inject the true path
        }
    });

    // Capture Standard Output (Python print statements, raw socket logs, info logs)
    backendProcess.stdout?.on('data', (data) => {
        backendChannel.append(data.toString());
    });

    // USE #2: Capture Standard Error (Python crashes, tracebacks)
    backendProcess.stderr?.on('data', (data) => {
        backendChannel.append(`[ERROR] ${data.toString()}`);
    });

    // USE #3: Process Monitoring
    backendProcess.on('close', (code) => {
        backendChannel.appendLine(`\n⚠️ Backend process exited with code ${code}`);
        // Optional: Trigger a VS Code notification so the user knows it crashed
        vscode.window.showErrorMessage(`Agent backend crashed (Code ${code}). Please reload the window.`);
    });
    
    backendProcess.on('error', (err) => {
        backendChannel.appendLine(`\n❌ Failed to start backend: ${err.message}`);
    });

    console.log('Local Agentic Workspace extension is now active.');

    // NOW that the process is booting up, connect the IPC client
    udsClient = new UdsClient();
    
    // (You will want a slight delay or retry-loop here so the binary has 
    // time to boot up and create the socket file before UdsClient connects)
    // 1. Initialize and connect the IPC Client
    connectWithRetry(udsClient)
        .then(() => {
            // Trigger background initial indexing on project load
            indexWorkspaceOnLoad(udsClient);
        })
        .catch(err => {
            vscode.window.showErrorMessage(`Failed to connect to orchestrator: ${err.message}`);
        });

    // ---------------------------------------------------------
    // PROACTIVE AI: Terminal Error Watcher
    // ---------------------------------------------------------
    context.subscriptions.push(
        vscode.window.onDidEndTerminalShellExecution(async (event) => {
            const exitCode = event.exitCode;
            
            // If exitCode is undefined, the command was cancelled. 
            // If it's > 0, the command failed.
            if (exitCode !== undefined && exitCode > 0) {
                const commandLine = event.execution.commandLine.value;
                
                // Ignore empty commands or simple typos that don't need AI
                if (!commandLine.trim()) return;

                console.log(`🚨 Terminal command failed: ${commandLine} (Exit Code: ${exitCode})`);

                // Extract the terminal output stream
                let terminalOutput = "";
                try {
                    // event.terminal.shellIntegration is required for this to work
                    for await (const data of event.execution.read()) {
                        terminalOutput += data;
                    }
                } catch (error) {
                    console.error("Failed to read terminal output", error);
                    terminalOutput = "Error reading terminal output stream.";
                }

                // Strip out excessive ANSI color codes for the LLM
                const cleanOutput = terminalOutput.replace(/\x1b\[[0-9;]*m/g, '').trim();

                // 🛡️ Interactive Auto-Fix Prompt
                const userAction = await vscode.window.showErrorMessage(
                    `Command failed (Exit ${exitCode}): ${commandLine}`,
                    'Auto-Fix with Agent',
                    'Dismiss'
                );

                if (userAction === 'Auto-Fix with Agent') {
                    // Instruct the LLM to format commands predictably
                    // Using Chain of Thought
                    const autoFixPrompt = `My terminal command failed with exit code ${exitCode}.\n\n` +
                                          `Command Executed:\n${commandLine}\n\n` +
                                          `Terminal Output (stderr):\n${cleanOutput}\n\n` +
                                          `Please diagnose the issue. CRITICAL INSTRUCTION: Consider the intent of the original command. If the command was a read-only or exploration command (like 'ls', 'cat', or 'echo'), DO NOT suggest creating missing files or directories unless absolutely necessary. Assume it was a typo and simply explain the error.\n\n` +
                                          "If the issue requires executing a new or different terminal command to fix:" +
                                                "1. Analyze the user's original intent and the current system context." +
                                                "2. Reason through the proposed command and its potential system impacts." +
                                                "3. If you lack high confidence in the command's safety or correctness, you must halt tool execution and ask the user for clarification before outputting the command."

                    vscode.window.showInformationMessage("🧠 Agent is diagnosing the terminal error...");
                    
                    try {
                        // Send the task to the agent orchestrator
                        const response = await udsClient.request("execute_agent_task", { goal: autoFixPrompt });
                        
                        // Extract the final observation summary
                        let summary = response.final_observation || response.summary || "Diagnosis complete.";
                        
                        // Check if the agent proposed a command to run
                        const commandMatch = summary.match(/<command>(.*?)<\/command>/);
                        
                        if (commandMatch) {
                            const proposedCommand = commandMatch[1].trim();
                            
                            // FIX: Replace the XML tags with markdown backticks so the command stays in the text!
                            summary = summary.replace(/<command>(.*?)<\/command>/g, '`$1`').trim();
                            
                            // Show the diagnosis and provide a button to run the command
                            const runAction = await vscode.window.showInformationMessage(
                                `Agent Diagnosis: ${summary}`,
                                `Run: ${proposedCommand}`,
                                'Dismiss'
                            );
                            
                            if (runAction === `Run: ${proposedCommand}`) {
                                // 🚀 Bring the terminal to the front, then execute the fix
                                event.terminal.show();
                                event.terminal.sendText(proposedCommand);
                            }
                        } else {
                            // Standard output if no command was proposed
                            vscode.window.showInformationMessage(`Agent Diagnosis: ${summary}`, { modal: true });
                        }
                    } catch (err: any) {
                        vscode.window.showErrorMessage(`Agent failed to diagnose: ${err.message}`);
                    }
                }
            }
        })
    );

    // We store the resolver here temporarily while the diff is open waiting for the user
    let pendingWriteResolve: ((value: { status: string }) => void) | null = null;

    // 1. Register CodeLens to the EXACT scheme used by your DiffProvider
    context.subscriptions.push(
        vscode.languages.registerCodeLensProvider(
            { scheme: AgenticDiffProvider.scheme }, 
            codeLensProvider
        )
    );

    // 2. The Internal Command called by UdsClient
    context.subscriptions.push(
        vscode.commands.registerCommand('agenticWorkspace.handleWriteRequest', async (filePath: string, newContent: string) => {
            return new Promise((resolve) => {
                pendingWriteResolve = resolve;

                const originalUri = vscode.Uri.file(filePath);
                const virtualUri = AgenticDiffProvider.getVirtualUri(originalUri);

                // Populate your existing virtual diff provider with the AI's content
                diffProvider.updateContent(virtualUri, newContent);

                // Open the diff view natively
                vscode.commands.executeCommand(
                    'vscode.diff',
                    originalUri,
                    virtualUri,
                    `Agent Proposed Changes ↔ ${path.basename(filePath)}`
                );

                // Tell the CodeLens provider to show the Accept/Reject buttons over the code
                codeLensProvider.setPendingState(true);
            });
        })
    );

    // 3. The Commands triggered when the user clicks the CodeLens buttons
    context.subscriptions.push(vscode.commands.registerCommand('agenticWorkspace.approveWrite', async () => {
        codeLensProvider.setPendingState(false);
        await vscode.commands.executeCommand('workbench.action.closeActiveEditor');
        
        // Note: We don't need to write the file here in TS.
        // We just tell Python "approved", and the Python file_system tool does the writing!
        if (pendingWriteResolve) pendingWriteResolve({ status: 'approved' });
    }));

    context.subscriptions.push(vscode.commands.registerCommand('agenticWorkspace.rejectWrite', async () => {
        codeLensProvider.setPendingState(false);
        await vscode.commands.executeCommand('workbench.action.closeActiveEditor');
        
        if (pendingWriteResolve) pendingWriteResolve({ status: 'denied' });
    }));

    // Create a dedicated Output Channel for our search results
    const searchOutputChannel = vscode.window.createOutputChannel('Local Agentic Search');
    context.subscriptions.push(searchOutputChannel);

    // 2. Start the Warp Proxy Server natively inside VS Code
    try {
        proxyServer = new WarpProxyServer();
        proxyServer.start();
        context.subscriptions.push({ dispose: () => proxyServer.stop() });
        console.log('Warp Proxy Server started successfully.');
    } catch (error: any) {
        vscode.window.showErrorMessage(`Failed to start proxy server: ${error.message}`);
    }

    // 2.5 Listen for file saves and sync to LanceDB
    context.subscriptions.push(
        vscode.workspace.onDidSaveTextDocument(async (document) => {
            // Only sync actual files (ignore output panels, settings, etc.)
            if (document.uri.scheme !== 'file') return;
            
            // Optional: Ignore massive minified files or build directories
            const filePath = document.uri.fsPath;
            if (filePath.includes('node_modules') || filePath.includes('out') || filePath.includes('.git')) {
                return;
            }

            try {
                await udsClient.request('sync_file', {
                    file_path: filePath,
                    content: document.getText()
                });
                console.log(`🧠 Synced ${path.basename(filePath)} to LanceDB`);
            } catch (error: any) {
                console.error(`Failed to sync ${filePath} to vector store:`, error);
            }
        })
    );

    // 3. Initialize the Status Bar (reads from config.json)
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBarItem.command = 'localAgenticWorkspace.showSettings'; // Clicking it opens settings!
    context.subscriptions.push(statusBarItem);
    updateStatusBar(); 

    // Watch config.json for changes so the status bar updates automatically
    const configWatcher = vscode.workspace.createFileSystemWatcher('**/services/orchestrator/config.json');
    configWatcher.onDidChange(() => updateStatusBar());
    configWatcher.onDidCreate(() => updateStatusBar());
    context.subscriptions.push(configWatcher);

    // 4. Register the Settings UI Command
    let disposableSettings = vscode.commands.registerCommand('localAgenticWorkspace.showSettings', async () => {
        const panel = vscode.window.createWebviewPanel(
            'agentSettings',
            'Agentic Workspace Settings',
            vscode.ViewColumn.One,
            {
                enableScripts: true, 
                localResourceRoots: [vscode.Uri.file(path.join(context.extensionPath, 'out'))]
            }
        );

        const scriptPathOnDisk = vscode.Uri.file(path.join(context.extensionPath, 'out', 'webview.js'));
        const scriptUri = panel.webview.asWebviewUri(scriptPathOnDisk);

        // Fetch models from the local Ollama API
        const fetchModels = (): Promise<string[]> => {
            return new Promise((resolve) => {
                http.get('http://127.0.0.1:11434/api/tags', (res) => {
                    let data = '';
                    res.on('data', chunk => data += chunk);
                    res.on('end', () => {
                        try {
                            const parsed = JSON.parse(data);
                            const models = parsed.models.map((m: any) => m.name);
                            resolve(models);
                        } catch (e) {
                            console.error('Failed to parse models', e);
                            resolve(['llama3:8b', 'qwen2.5-coder:7b']); // Fallback defaults
                        }
                    });
                }).on('error', (e) => {
                    console.error('Failed to reach local LLM API', e);
                    resolve(['llama3:8b', 'qwen2.5-coder:7b']); // Fallback defaults
                });
            });
        };

        panel.webview.html = `
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Settings</title>
            </head>
            <body>
                <div id="root"></div>
                <script src="${scriptUri}"></script>
            </body>
            </html>
        `;

        panel.webview.onDidReceiveMessage(async (message) => {
            if (message.command === 'ready') {
                // 1. When React tells us it's mounted, fetch and send the models
                const availableModels = await fetchModels();
                panel.webview.postMessage({ 
                    command: 'loadModels', 
                    models: availableModels 
                });
                // 2. Fetch the true configuration from the Python orchestrator
                try {
                    const configData = await udsClient.request('get_config', {});
                    panel.webview.postMessage({
                        command: 'loadConfig',
                        config: configData
                    });
                } catch (error: any) {
                    console.error('Failed to fetch config from backend:', error);
                    vscode.window.showErrorMessage(`Could not load current settings: ${error.message}`);

                    // 🛡️ FAILSAFE: Tell React to stop loading and fall back to defaults
                    panel.webview.postMessage({
                        command: 'loadConfig',
                        config: null 
                    });
                }
            } else if (message.command === 'updateSetting') {
                try {
                    await udsClient.request('update_config', message.config);
                    vscode.window.showInformationMessage('Settings successfully synced to Orchestrator!');
                } catch (error: any) {
                    vscode.window.showErrorMessage(`Failed to sync settings: ${error.message}`);
                }
            } 
        });
    });
    context.subscriptions.push(disposableSettings);

    // 5. Register the Chat Sidebar Provider
    const chatProvider = new ChatViewProvider(context.extensionUri, udsClient);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(ChatViewProvider.viewType, chatProvider)
    );

    // 6. Initialize the AST Provider
    const astProvider = new ASTProvider(context.extensionUri);

    // 7. Register the Diff Provider
    const diffProvider = new AgenticDiffProvider();
    context.subscriptions.push(
        vscode.workspace.registerTextDocumentContentProvider(
            AgenticDiffProvider.scheme, 
            diffProvider
        )
    );

    // 8. Register the AST Test Command (from Task 2.2)
    const testAstCommand = vscode.commands.registerCommand('localAgentic.testAST', async () => {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showErrorMessage('No active editor found to parse.');
            return;
        }

        try {
            vscode.window.showInformationMessage(`Running AST parser on ${path.basename(editor.document.fileName)}...`);
            
            // Call your AST provider here. Adjust the method name if it's not "parseDocument"
            const parsedTree = await astProvider.parseDocument(editor.document);
            
            console.log(`🌲 AST Output for ${path.basename(editor.document.fileName)}:`);
            console.log(parsedTree);
            
            vscode.window.showInformationMessage('AST parsing complete! Check the Debug Console.');
        } catch (error: any) {
            console.error('AST Error:', error);
            vscode.window.showErrorMessage(`AST Parsing failed: ${error.message}`);
        }
    });
    context.subscriptions.push(testAstCommand)

    // 9. Register the Semantic Diff Command
    const showDiffCommand = vscode.commands.registerCommand('localAgentic.showProposedDiff', async () => {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showErrorMessage('No active editor found to diff against.');
            return;
        }

        const originalUri = editor.document.uri;
        const virtualUri = AgenticDiffProvider.getVirtualUri(originalUri);

        const originalText = editor.document.getText();
        const mockAiProposedText = "// -- AI PROPOSED REFACTOR --\n\n" + originalText.split('\n').reverse().join('\n');

        diffProvider.updateContent(virtualUri, mockAiProposedText);

        // Tell the CodeLens provider to show the buttons
        codeLensProvider.setPendingState(true);

        await vscode.commands.executeCommand(
            'vscode.diff',
            originalUri,                 
            virtualUri,                  
            `AI Proposal: ${path.basename(originalUri.fsPath)}`, 
            { preview: true }            
        );
    });
    context.subscriptions.push(showDiffCommand);

    // Register a command to test Semantic Search latency
    const testSearchCommand = vscode.commands.registerCommand('localAgentic.searchCodebase', async () => {
        const query = await vscode.window.showInputBox({
            prompt: 'Enter a semantic query to search your codebase'
        });

        if (!query) return;

        try {
            const response = await udsClient.request('search_codebase', { query: query, limit: 3 });
            
            searchOutputChannel.clear();
            searchOutputChannel.show(true); 
            
            searchOutputChannel.appendLine(`=========================================`);
            searchOutputChannel.appendLine(`🔍 Search Results for: "${query}"`);
            searchOutputChannel.appendLine(`⏱️  Round-trip time: ${response.elapsed_ms}ms`);
            searchOutputChannel.appendLine(`=========================================\n`);
            
            response.results.forEach((res: any, index: number) => {
                searchOutputChannel.appendLine(`[Result ${index + 1}] Distance: ${res.score.toFixed(3)}`);
                searchOutputChannel.appendLine(`File: ${res.file_path}\n`);
                searchOutputChannel.appendLine(res.content);
                searchOutputChannel.appendLine(`\n-----------------------------------------\n`);
            });

        } catch (error: any) {
            vscode.window.showErrorMessage(`Search failed: ${error.message}`);
        }
    });
    context.subscriptions.push(testSearchCommand);
}

// Helper to refresh the status bar text directly from config.json
function updateStatusBar() {
    try {
        // 1. Set our fallback defaults
        let profileName = 'home';
        let model = 'llama3:8b';
        let endpoint = 'http://127.0.0.1:11434/v1/chat/completions';

        // 2. Try to read from the JSON file if it exists
        const workspaceFolders = vscode.workspace.workspaceFolders;
        if (workspaceFolders) {
            const configPath = path.join(workspaceFolders[0].uri.fsPath, 'services', 'orchestrator', 'config.json');
            
            if (fs.existsSync(configPath)) {
                const configData = JSON.parse(fs.readFileSync(configPath, 'utf8'));
                profileName = configData.active_profile || profileName;
                const profileSettings = configData.profiles?.[profileName];

                if (profileSettings) {
                    model = profileSettings.llm?.model_name || model;
                    endpoint = profileSettings.llm?.endpoint_url || endpoint;
                }
            }
        }

        // 3. Format and unconditionally show the status bar item
        const formattedName = profileName.charAt(0).toUpperCase() + profileName.slice(1);
        statusBarItem.text = `$(hubot) ${formattedName}`;
        statusBarItem.tooltip = `Model: ${model}\nEndpoint: ${endpoint}\nClick to open settings.`;
        statusBarItem.show();

    } catch (err) {
        console.error('Failed to update status bar:', err);
    }
}

async function indexWorkspaceOnLoad(udsClient: UdsClient) {
    // Find all relevant source files, ignoring build/node_modules directories
    const files = await vscode.workspace.findFiles(
        '**/*.{ts,tsx,py,js,md,json}',
        '**/{node_modules,out,dist,.git,.lancedb,.venv}/**'
    );

    console.log(`🚀 Starting initial workspace index: ${files.length} files found.`);

    for (const fileUri of files) {
        try {
            const document = await vscode.workspace.openTextDocument(fileUri);
            await udsClient.request('sync_file', {
                file_path: fileUri.fsPath,
                content: document.getText()
            });
        } catch (error) {
            console.error(`Failed to index on load: ${fileUri.fsPath}`, error);
        }
    }

    console.log('✅ Workspace initial indexing complete.');
}

export function deactivate() {
    console.log('Deactivating Local Agentic Workspace.');
    if (proxyServer) {
        proxyServer.stop();
    }
    // Kill the phantom binary!
    if (backendProcess && !backendProcess.killed) {
        console.log('Terminating backend process...');
        backendProcess.kill(); 
    }
}

async function connectWithRetry(client: UdsClient, retries = 20, delayMs = 500): Promise<void> {
    for (let i = 0; i < retries; i++) {
        try {
            await client.connect();
            console.log('✅ Successfully connected to UDS socket.');
            return; 
        } catch (err: any) {
            // If the file doesn't exist yet, or the connection is refused, wait and try again
            if (err.code === 'ENOENT' || err.code === 'ECONNREFUSED') {
                console.log(`Socket not ready yet, retrying in ${delayMs}ms... (${i + 1}/${retries})`);
                await new Promise(resolve => setTimeout(resolve, delayMs));
            } else {
                throw err; // A real error occurred
            }
        }
    }
    throw new Error("Timeout waiting for Python orchestrator to initialize the socket.");
}
