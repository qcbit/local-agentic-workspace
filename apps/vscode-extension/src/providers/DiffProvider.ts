import * as vscode from 'vscode';

export class AgenticDiffProvider implements vscode.TextDocumentContentProvider {
    public static readonly scheme = 'localAgentic-diff';
    
    // Emitter to notify VS Code when the virtual document content updates
    private onDidChangeEmitter = new vscode.EventEmitter<vscode.Uri>();
    onDidChange = this.onDidChangeEmitter.event;

    // In-memory storage for the AI's proposed code changes
    private contentMap = new Map<string, string>();

    /**
     * Called by VS Code to resolve the content of our virtual URI
     */
    provideTextDocumentContent(uri: vscode.Uri): string {
        // Return the stored proposed code, or an empty string if not found
        return this.contentMap.get(uri.toString()) || '';
    }

    /**
     * Stores the AI's proposed code and triggers an update
     */
    public updateContent(uri: vscode.Uri, content: string) {
        this.contentMap.set(uri.toString(), content);
        this.onDidChangeEmitter.fire(uri);
    }

    /**
     * Helper to generate a virtual URI for a given file
     */
    public static getVirtualUri(originalUri: vscode.Uri): vscode.Uri {
        return originalUri.with({
            scheme: AgenticDiffProvider.scheme,
            path: originalUri.path,
            query: `timestamp=${Date.now()}` // Bust cache to ensure fresh renders
        });
    }
}
