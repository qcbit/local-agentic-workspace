import * as vscode from 'vscode';
import { ASTProvider } from './ast/ASTProvider';

export async function activate(context: vscode.ExtensionContext) {
    console.log('Local Agentic Workspace extension is now active.');

    // Initialize AST Provider in the background
    const astProvider = new ASTProvider(context.extensionUri);
    astProvider.initialize().catch(err => console.error("AST Init Failed", err));

    // Register a command to manually trigger AST extraction for testing
    context.subscriptions.push(
        vscode.commands.registerCommand('localAgentic.testAST', async () => {
            const editor = vscode.window.activeTextEditor;
            if (editor) {
                const astData = await astProvider.parseDocument(editor.document);
                console.log('Extracted Context:', astData);
                vscode.window.showInformationMessage(`Extracted ${astData.symbols?.length || 0} symbols.`);
            }
        })
    );
}

export function deactivate() {}
