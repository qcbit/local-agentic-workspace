import * as vscode from 'vscode';
import * as path from 'path';
import { ASTProvider } from './ast/ASTProvider';
import { AgenticDiffProvider } from './providers/DiffProvider';
import { WarpProxyServer } from './proxy/WarpProxyServer';

export function activate(context: vscode.ExtensionContext) {
    console.log('Local Agentic Workspace extension is now active.');

    // 1. Start the Warp Proxy Server (Task 2.4)
    const proxyServer = new WarpProxyServer();
    proxyServer.start();
    
    // Ensure the server shuts down when the extension deactivates
    context.subscriptions.push({ dispose: () => proxyServer.stop() });

    // 2. Initialize the AST Provider (from Task 2.2)
    const astProvider = new ASTProvider(context.extensionUri);

    // 3. Register the Diff Provider (Task 2.3)
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

    context.subscriptions.push(testAstCommand, showDiffCommand);
}

export function deactivate() {}
