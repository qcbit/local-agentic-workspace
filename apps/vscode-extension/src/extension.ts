import * as vscode from 'vscode';
import * as path from 'path';
import { ASTProvider } from './ast/ASTProvider';
import { AgenticDiffProvider } from './providers/DiffProvider';
import { UdsClient } from './ipc/UdsClient';
import { WarpProxyServer } from './proxy/WarpProxyServer';

let statusBarItem: vscode.StatusBarItem;
let udsClient: UdsClient;

export function activate(context: vscode.ExtensionContext) {
    console.log('Local Agentic Workspace extension is now active.');

    // 1. Initialize and connect the IPC Client
    udsClient = new UdsClient();
    udsClient.connect().catch(err => {
        vscode.window.showErrorMessage(`Failed to connect to orchestrator: ${err.message}`);
    });

    // 2. Register the Settings UI Command
    let disposable = vscode.commands.registerCommand('localAgenticWorkspace.showSettings', () => {
        const panel = vscode.window.createWebviewPanel(
            'agentSettings',
            'Agentic Workspace Settings',
            vscode.ViewColumn.One,
            {
                enableScripts: true, // Required for React
                localResourceRoots: [vscode.Uri.file(path.join(context.extensionPath, 'out'))]
            }
        );

        // Get path to the compiled React bundle
        const scriptPathOnDisk = vscode.Uri.file(path.join(context.extensionPath, 'out', 'webview.js'));
        const scriptUri = panel.webview.asWebviewUri(scriptPathOnDisk);

        // Inject the HTML shell
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

        // 3. Listen for the 'Save' button click from React
        panel.webview.onDidReceiveMessage(async (message) => {
            if (message.command === 'updateSetting') {
                try {
                    // Send the new config down the socket to Python
                    await udsClient.request('update_config', message.config);
                    vscode.window.showInformationMessage('Settings successfully synced to Orchestrator!');
                } catch (error: any) {
                    vscode.window.showErrorMessage(`Failed to sync settings: ${error.message}`);
                }
            }
        });
    });

    // 1. Start the Warp Proxy Server
    const proxyServer = new WarpProxyServer();
    proxyServer.start();
    context.subscriptions.push({ dispose: () => proxyServer.stop() });

    // 2. Setup the BYOA Status Bar Item
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBarItem.command = 'localAgentic.switchProfile';
    context.subscriptions.push(statusBarItem);
    updateStatusBar(); // Initial render
    statusBarItem.show();

    // Listen for manual settings changes to keep the status bar in sync
    context.subscriptions.push(
        vscode.workspace.onDidChangeConfiguration(e => {
            if (e.affectsConfiguration('localAgentic.activeProfile')) {
                updateStatusBar();
            }
        })
    );

    // 3. Command to switch profiles via QuickPick
    const switchProfileCommand = vscode.commands.registerCommand('localAgentic.switchProfile', async () => {
        const config = vscode.workspace.getConfiguration('localAgentic');
        const profiles = config.get<any[]>('profiles') || [];
        
        const items = profiles.map(p => ({
            label: p.name,
            description: p.model,
            profileId: p.id
        }));

        const selected = await vscode.window.showQuickPick(items, {
            placeHolder: 'Select active AI routing profile'
        });

        if (selected) {
            await config.update('activeProfile', selected.profileId, vscode.ConfigurationTarget.Global);
            vscode.window.showInformationMessage(`Switched AI Profile to: ${selected.label}`);
        }
    });
    
    // Ensure the server shuts down when the extension deactivates
    context.subscriptions.push({ dispose: () => proxyServer.stop() });

    // 2. Initialize the AST Provider
    const astProvider = new ASTProvider(context.extensionUri);

    // 3. Register the Diff Provider
    const diffProvider = new AgenticDiffProvider();
    context.subscriptions.push(
        vscode.workspace.registerTextDocumentContentProvider(
            AgenticDiffProvider.scheme, 
            diffProvider
        )
    );

    // 4. Register the AST Test Command (from Task 2.2)
    const testAstCommand = vscode.commands.registerCommand('localAgentic.testAST', async () => {
        // ... (Keep your existing AST test code here)
    });

    // 5. Register the Semantic Diff Command
    const showDiffCommand = vscode.commands.registerCommand('localAgentic.showProposedDiff', async () => {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showErrorMessage('No active editor found to diff against.');
            return;
        }

        const originalUri = editor.document.uri;
        const virtualUri = AgenticDiffProvider.getVirtualUri(originalUri);

        // MOCK AI RESPONSE: For testing, let's reverse the active file's text 
        // to simulate the AI returning a "modified" version of the file.
        const originalText = editor.document.getText();
        const mockAiProposedText = "// -- AI PROPOSED REFACTOR --\n\n" + originalText.split('\n').reverse().join('\n');

        // Update the virtual document with the AI's code
        diffProvider.updateContent(virtualUri, mockAiProposedText);

        // Launch the native VS Code diff window
        await vscode.commands.executeCommand(
            'vscode.diff',
            originalUri,                 // Left side (Current file)
            virtualUri,                  // Right side (AI proposed file)
            `AI Proposal: ${path.basename(originalUri.fsPath)}`, // Tab Title
            { preview: true }            // Keep it as a preview tab
        );
    });

    context.subscriptions.push(testAstCommand, showDiffCommand, switchProfileCommand, disposable);
}

// Helper to refresh the status bar text
function updateStatusBar() {
    const config = vscode.workspace.getConfiguration('localAgentic');
    const activeId = config.get<string>('activeProfile');
    const profiles = config.get<any[]>('profiles') || [];
    const activeProfile = profiles.find(p => p.id === activeId) || profiles[0];

    if (activeProfile) {
        statusBarItem.text = `$(hubot) ${activeProfile.name}`;
        statusBarItem.tooltip = `Model: ${activeProfile.model}\nEndpoint: ${activeProfile.endpointUrl}`;
    }
}

// Conceptual snippet
// webviewPanel.webview.onDidReceiveMessage(async (message) => {
//     if (message.command === 'updateSetting') {
//         // 1. Format the JSON-RPC payload
//         const payload = {
//             jsonrpc: "2.0",
//             method: "update_config",
//             params: message.config,
//             id: Date.now()
//         };
        
//         // 2. Push down to the Python backend via /tmp/agent.sock
//         udsClient.write(JSON.stringify(payload) + '\n');
        
//         vscode.window.showInformationMessage('Settings synced to local model!');
//     }
// });

export function deactivate() {
    // Graceful cleanup
    console.log('Deactivating Local Agentic Workspace.');
}
