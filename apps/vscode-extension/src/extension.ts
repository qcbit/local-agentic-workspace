import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as http from 'http';
import { ASTProvider } from './ast/ASTProvider';
import { AgenticDiffProvider } from './providers/DiffProvider';
import { UdsClient } from './ipc/UdsClient';
import { WarpProxyServer } from './proxy/WarpProxyServer';
import { ChatViewProvider } from './providers/ChatViewProvider';
import { AgentApprovalCodeLensProvider } from './codelens';

let udsClient: UdsClient;
let proxyServer: WarpProxyServer;
let statusBarItem: vscode.StatusBarItem;
const codeLensProvider = new AgentApprovalCodeLensProvider();

export function activate(context: vscode.ExtensionContext) {
    console.log('Local Agentic Workspace extension is now active.');

    // 1. Initialize and connect the IPC Client
    udsClient = new UdsClient();
    udsClient.connect().then(() => {
        // Trigger background initial indexing on project load
        indexWorkspaceOnLoad(udsClient);
    }).catch(err => {
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

                // Push the notification to the Python orchestrator
                udsClient.sendNotification("terminal_error_detected", {
                    command: commandLine,
                    exit_code: exitCode,
                    error_output: cleanOutput
                });
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

    // ... 
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
}
